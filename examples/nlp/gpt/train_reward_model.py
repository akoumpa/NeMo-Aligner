# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
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

import torch.multiprocessing as mp
from omegaconf.omegaconf import OmegaConf

from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager
from nemo_aligner.algorithms.supervised import SupervisedTrainer
from nemo_aligner.data.nlp.builders import build_dataloader, build_train_valid_test_rm_datasets
from nemo_aligner.models.nlp.gpt.megatron_gpt_reward_model import MegatronGPTRewardModel
from nemo_aligner.utils.train_script_utils import (
    CustomLoggerWrapper,
    add_custom_checkpoint_callback,
    extract_optimizer_scheduler_from_ptl_model,
    init_distributed,
    init_using_ptl,
    resolve_and_create_trainer,
    retrieve_custom_trainer_state_dict,
)
from nemo_aligner.utils.utils import load_and_override_model_config, load_from_nemo

"""Script to start Reward Model training"""

OmegaConf.register_new_resolver("multiply", lambda x, y: x * y, replace=True)
OmegaConf.register_new_resolver("int_div", lambda x, y: x // y, replace=True)

mp.set_start_method("spawn", force=True)


@hydra_runner(config_path="conf", config_name="training_rm")
def main(cfg) -> None:
    cfg.model = load_and_override_model_config(cfg.pretrained_checkpoint.restore_from_path, cfg.model)

    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f"\n{OmegaConf.to_yaml(cfg)}")

    trainer = resolve_and_create_trainer(cfg, "rm")
    exp_manager(trainer, cfg.exp_manager)
    logger = CustomLoggerWrapper(trainer.loggers)

    ptl_model = load_from_nemo(
        MegatronGPTRewardModel,
        cfg.model,
        trainer,
        strict=True,
        load_base_model_only=True,
        restore_path=cfg.pretrained_checkpoint.restore_from_path,
    )

    # pull values from checkpoint
    trainer_restore_path = trainer.ckpt_path
    if trainer_restore_path is not None:
        custom_trainer_state_dict = retrieve_custom_trainer_state_dict(trainer)
        consumed_samples = custom_trainer_state_dict["consumed_samples"]
    else:
        custom_trainer_state_dict = None
        consumed_samples = 0

    init_distributed(trainer, ptl_model, cfg.model.get("transformer_engine", False))

    # use the entire dataset
    train_valid_test_num_samples = [-1 * cfg.model.global_batch_size] * 3

    train_ds, validation_ds, _ = build_train_valid_test_rm_datasets(
        cfg=cfg.model,
        data_prefix=cfg.model.data.data_prefix,
        data_impl=cfg.model.data.data_impl,
        splits_string=cfg.model.data.splits_string,
        train_valid_test_num_samples=train_valid_test_num_samples,
        seq_length=cfg.model.data.seq_length,
        seed=cfg.model.seed,
        tokenizer=ptl_model.tokenizer,
    )

    train_dataloader = build_dataloader(
        cfg=cfg,
        dataset=train_ds,
        consumed_samples=consumed_samples,
        mbs=cfg.model.micro_batch_size,
        gbs=cfg.model.global_batch_size,
        load_gbs=True,
    )

    val_dataloader = build_dataloader(
        cfg=cfg,
        dataset=validation_ds,
        consumed_samples=0,
        mbs=cfg.model.micro_batch_size,
        gbs=cfg.model.global_batch_size,
        load_gbs=True,
    )

    init_using_ptl(trainer, ptl_model, train_dataloader, train_ds)
    optimizer, scheduler = extract_optimizer_scheduler_from_ptl_model(ptl_model)

    ckpt_callback = add_custom_checkpoint_callback(trainer, ptl_model)

    logger.log_hyperparams(OmegaConf.to_container(cfg))

    rm_trainer = SupervisedTrainer(
        cfg=cfg.trainer.rm,
        model=ptl_model,
        optimizer=optimizer,
        scheduler=scheduler,
        train_dataloader=train_dataloader,
        val_dataloader=val_dataloader,
        test_dataloader=None,
        logger=logger,
        ckpt_callback=ckpt_callback,
    )

    if custom_trainer_state_dict is not None:
        rm_trainer.load_state_dict(custom_trainer_state_dict)

    rm_trainer.fit()


if __name__ == "__main__":
    main()
