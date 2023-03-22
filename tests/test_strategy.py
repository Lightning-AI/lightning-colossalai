# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

import pytest
import torch
import torch.nn.functional as F  # noqa: N812
from colossalai.nn.optimizer import HybridAdam
from lightning_utilities.core.imports import RequirementCache, module_available
from torch import Tensor, nn
from torchmetrics import Accuracy

if module_available("lightning"):
    from lightning.pytorch import LightningModule, Trainer, seed_everything
    from lightning.pytorch.callbacks import ModelCheckpoint
    from lightning.pytorch.demos.boring_classes import BoringModel
elif module_available("pytorch_lightning"):
    from pytorch_lightning import LightningModule, Trainer, seed_everything
    from pytorch_lightning.callbacks import ModelCheckpoint
    from pytorch_lightning.demos.boring_classes import BoringModel

import lightning_colossalai.strategy
from lightning_colossalai import ColossalAIPrecisionPlugin, ColossalAIStrategy
from tests import RunIf
from tests.datamodules import ClassifDataModule

_TM_GE_0_11 = RequirementCache("torchmetrics>=0.11.0")


def test_invalid_colossalai(monkeypatch):
    monkeypatch.setattr(lightning_colossalai.strategy, "_COLOSSALAI_AVAILABLE", False)
    with pytest.raises(
        ModuleNotFoundError,
        match="To use the `ColossalAIStrategy`, please install `colossalai` first. "
        "Download `colossalai` by consulting `https://colossalai.org/download`.",
    ):
        ColossalAIStrategy()


@pytest.mark.parametrize("by", ("colossalai", ColossalAIStrategy()))
def test_colossalai_strategy_with_trainer(by):
    trainer = Trainer(precision="16-mixed", strategy=by)
    assert isinstance(trainer.strategy, ColossalAIStrategy)
    assert isinstance(trainer.strategy.precision_plugin, ColossalAIPrecisionPlugin)


class ModelParallelBoringModel(BoringModel):
    def __init__(self):
        super().__init__()
        self.layer = None

    def configure_sharded_model(self) -> None:
        self.layer = torch.nn.Linear(32, 2)

    def configure_optimizers(self):
        optimizer = HybridAdam(self.layer.parameters(), lr=1e-3)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
        return [optimizer], [lr_scheduler]


class ModelParallelBoringModelNoSchedulers(ModelParallelBoringModel):
    def configure_optimizers(self):
        return HybridAdam(self.layer.parameters(), lr=1e-3)


@RunIf(min_gpus=1)
def test_gradient_clip_algorithm_error(tmpdir):
    model = ModelParallelBoringModel()
    trainer = Trainer(
        fast_dev_run=True,
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=1,
        precision="16-mixed",
        strategy="colossalai",
        enable_progress_bar=False,
        enable_model_summary=False,
        gradient_clip_val=1.0,
        gradient_clip_algorithm="value",
    )
    with pytest.raises(NotImplementedError, match="`clip_grad_by_value` is not supported by `ColossalAI`"):
        trainer.fit(model)


@RunIf(min_cuda_gpus=1, standalone=True)
def test_colossalai_optimizer(tmpdir):
    class UnsupportedOptimizerModel(ModelParallelBoringModel):
        def configure_optimizers(self):
            return torch.optim.Adam(self.parameters())

    trainer = Trainer(
        fast_dev_run=True,
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=1,
        precision="16-mixed",
        strategy="colossalai",
        enable_progress_bar=False,
        enable_model_summary=False,
    )
    with pytest.raises(
        ValueError,
        match="`ColossalAIStrategy` only supports `colossalai.nn.optimizer.CPUAdam` "
        "and `colossalai.nn.optimizer.HybridAdam` as its optimizer.",
    ):
        trainer.fit(UnsupportedOptimizerModel())


@RunIf(min_cuda_gpus=1, standalone=True)
def test_warn_colossalai_ignored(tmpdir):
    class TestModel(ModelParallelBoringModel):
        def backward(self, loss: Tensor, *args, **kwargs) -> None:
            return loss.backward()

    model = TestModel()
    trainer = Trainer(
        fast_dev_run=True,
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=1,
        precision="16-mixed",
        strategy="colossalai",
        enable_progress_bar=False,
        enable_model_summary=False,
    )

    with pytest.warns(UserWarning, match="will be ignored since ColossalAI handles the backward"):
        trainer.fit(model)


@RunIf(min_cuda_gpus=1, standalone=True)
def test_configure_sharded_model_hook_not_overridden(tmpdir):
    class TestModel(BoringModel):
        def configure_optimizers(self):
            return HybridAdam(self.layer.parameters(), lr=1e-3)

    model = TestModel()
    trainer = Trainer(
        fast_dev_run=True,
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=1,
        precision="16-mixed",
        strategy="colossalai",
        enable_progress_bar=False,
        enable_model_summary=False,
    )

    with pytest.raises(TypeError, match="your LightningModule must override the `configure_sharded_model`"):
        trainer.fit(model)


def _assert_save_model_is_equal(model, tmpdir, trainer):
    checkpoint_path = os.path.join(tmpdir, "model.pt")
    checkpoint_path = trainer.strategy.broadcast(checkpoint_path)
    trainer.save_checkpoint(checkpoint_path)
    trainer.strategy.barrier()

    # carry out the check only on rank 0
    if trainer.is_global_zero:
        state_dict = torch.load(checkpoint_path)

        # Assert model parameters are identical after loading
        for orig_param, saved_model_param in zip(model.parameters(), state_dict.values()):
            saved_model_param = saved_model_param.to(dtype=orig_param.dtype, device=orig_param.device)
            assert torch.equal(orig_param, saved_model_param)


class ModelParallelClassificationModel(LightningModule):
    def __init__(self, lr=0.01):
        super().__init__()

        self.lr = lr
        self.layers = None

        metric = Accuracy(task="multiclass", num_classes=3) if _TM_GE_0_11 else Accuracy()
        self.train_acc = metric.clone()
        self.valid_acc = metric.clone()
        self.test_acc = metric.clone()

    def build_layers(self) -> nn.Module:
        layers = []
        for _ in range(3):
            layers.append(nn.Linear(32, 32))
            layers.append(nn.ReLU())
        layers.append(nn.Linear(32, 3))
        return nn.Sequential(*layers)

    def configure_sharded_model(self) -> None:
        if self.layers is None:
            self.layers = self.build_layers()

    def forward(self, x):
        x = self.layers(x)
        logits = F.softmax(x, dim=1)
        return logits

    def configure_optimizers(self):
        optimizer = HybridAdam(self.parameters(), lr=self.lr)
        return [optimizer], []

    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = F.cross_entropy(logits, y)
        self.log("train_loss", loss, prog_bar=True, sync_dist=True)
        self.log("train_acc", self.train_acc(logits, y), prog_bar=True, sync_dist=True)
        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        self.log("val_loss", F.cross_entropy(logits, y), prog_bar=False, sync_dist=True)
        self.log("val_acc", self.valid_acc(logits, y), prog_bar=True, sync_dist=True)

    def test_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        self.log("test_loss", F.cross_entropy(logits, y), prog_bar=False, sync_dist=True)
        self.log("test_acc", self.test_acc(logits, y), prog_bar=True, sync_dist=True)

    def predict_step(self, batch, batch_idx):
        x, _ = batch
        return self.forward(x)


@RunIf(min_cuda_gpus=2, standalone=True)
def test_multi_gpu_checkpointing(tmpdir):
    dm = ClassifDataModule()
    model = ModelParallelClassificationModel()
    ck = ModelCheckpoint(monitor="val_acc", mode="max", save_last=True, save_top_k=-1)

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=1,
        accelerator="gpu",
        devices=2,
        precision="16-mixed",
        strategy="colossalai",
        callbacks=[ck],
        num_sanity_val_steps=0,  # TODO: remove once validation/test before fitting is supported again
    )
    trainer.fit(model, datamodule=dm)

    results = trainer.test(datamodule=dm)
    saved_results = trainer.test(ckpt_path=ck.best_model_path, datamodule=dm)
    assert saved_results == results


@RunIf(min_gpus=2, standalone=True)
def test_run_trainer_test_without_fit(tmpdir):
    model = ModelParallelClassificationModel()
    dm = ClassifDataModule()
    trainer = Trainer(
        default_root_dir=tmpdir, accelerator="gpu", devices=2, precision="16-mixed", strategy="colossalai"
    )

    # Colossal requires warmup, you can't run validation/test without having fit first
    # This is a temporary limitation
    trainer.test(model, datamodule=dm)


@RunIf(min_gpus=2, standalone=True)
def test_multi_gpu_model_colossalai_fit_test(tmpdir):
    seed_everything(7)

    dm = ClassifDataModule()
    model = ModelParallelClassificationModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=2,
        precision="16-mixed",
        strategy=ColossalAIStrategy(initial_scale=32),
        max_epochs=1,
        num_sanity_val_steps=0,  # TODO: remove once validation/test before fitting is supported again
    )
    trainer.fit(model, datamodule=dm)

    if trainer.is_global_zero:
        out_metrics = trainer.callback_metrics
        assert out_metrics["train_acc"].item() > 0.7
        assert out_metrics["val_acc"].item() > 0.7

    result = trainer.test(model, datamodule=dm)
    if trainer.is_global_zero:
        for out in result:
            assert out["test_acc"] > 0.7
