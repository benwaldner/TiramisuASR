# Copyright 2020 Huy Le Nguyen (@usimarit)
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

import tensorflow as tf
import tensorflow.keras.mixed_precision.experimental as mixed_precision

from .base_runners import BaseTrainer
from ..losses.rnnt_losses import rnnt_loss
from ..models.transducer import Transducer
from ..featurizers.text_featurizers import TextFeaturizer


class TransducerTrainer(BaseTrainer):
    def __init__(self,
                 config: dict,
                 text_featurizer: TextFeaturizer,
                 is_mixed_precision: bool = False,
                 strategy: tf.distribute.Strategy = None):
        """
        Args:
            config: the 'running_config' part in YAML config file'
            text_featurizer: the TextFeaturizer instance
            is_mixed_precision: a boolean for using mixed precision or not
        """
        super(TransducerTrainer, self).__init__(config, strategy=strategy)
        self.text_featurizer = text_featurizer
        self.is_mixed_precision = is_mixed_precision

    def set_train_metrics(self):
        self.train_metrics = {
            "transducer_loss": tf.keras.metrics.Mean("train_transducer_loss", dtype=tf.float32)
        }

    def set_eval_metrics(self):
        self.eval_metrics = {
            "transducer_loss": tf.keras.metrics.Mean("eval_transducer_loss", dtype=tf.float32)
        }

    @tf.function(experimental_relax_shapes=True)
    def _train_step(self, batch):
        _, features, input_length, labels, label_length, pred_inp = batch

        with tf.GradientTape() as tape:
            logits = self.model([features, pred_inp], training=True)
            tape.watch(logits)
            per_train_loss = rnnt_loss(
                logits=logits, labels=labels, label_length=label_length,
                logit_length=(input_length // self.model.time_reduction_factor),
                blank=self.text_featurizer.blank
            )
            train_loss = tf.nn.compute_average_loss(per_train_loss,
                                                    global_batch_size=self.global_batch_size)

            if self.is_mixed_precision:
                scaled_train_loss = self.optimizer.get_scaled_loss(train_loss)

        if self.is_mixed_precision:
            scaled_gradients = tape.gradient(scaled_train_loss, self.model.trainable_variables)
            gradients = self.optimizer.get_unscaled_gradients(scaled_gradients)
        else:
            gradients = tape.gradient(train_loss, self.model.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.model.trainable_variables))

        self.train_metrics["transducer_loss"].update_state(per_train_loss)

    @tf.function(experimental_relax_shapes=True)
    def _eval_step(self, batch):
        _, features, input_length, labels, label_length, pred_inp = batch

        logits = self.model([features, pred_inp], training=False)
        eval_loss = rnnt_loss(
            logits=logits, labels=labels, label_length=label_length,
            logit_length=(input_length // self.model.time_reduction_factor),
            blank=self.text_featurizer.blank
        )

        self.eval_metrics["transducer_loss"].update_state(eval_loss)

    def compile(self,
                model: Transducer,
                optimizer: any,
                max_to_keep: int = 10):
        with self.strategy.scope():
            self.model = model
            self.model.summary(line_length=100)
            self.optimizer = tf.keras.optimizers.get(optimizer)
            if self.is_mixed_precision:
                self.optimizer = mixed_precision.LossScaleOptimizer(self.optimizer, "dynamic")
        self.create_checkpoint_manager(max_to_keep, model=self.model, optimizer=self.optimizer)

    def fit(self, train_dataset, eval_dataset=None, eval_train_ratio=1):
        self.set_train_data_loader(train_dataset)
        self.set_eval_data_loader(eval_dataset, eval_train_ratio)
        self.load_checkpoint()
        self.run()
