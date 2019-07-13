import tensorflow as tf
import time
from model import transformer
from preprocess import get_vocab
from model.learning_rate_schedule import CustomSchedule
from pathlib import Path

# tf.enable_eager_execution()

HPARAMS = {
    "num_layers": 1,
    "d_model": 128,
    "num_heads": 8,
    "dff": 512,
    "dropout_rate": 0.1,
    "learning_rate": 0.01
}


def get_dataset(dataset_path: Path, batch_size: int, shuffle_buffer: int, prefetch_buffer: int):
    feature_description = {
        'text': tf.io.VarLenFeature(tf.int64)
    }

    def _parse_function(example_proto):
        example = tf.io.parse_single_example(example_proto, feature_description)
        return tf.sparse.to_dense(example["text"])

    ds = tf.data.TFRecordDataset(str(dataset_path))
    ds = ds.map(_parse_function)
    ds = ds.shuffle(buffer_size=shuffle_buffer)
    ds = ds.padded_batch(batch_size, padded_shapes=(-1,))
    ds = ds.prefetch(buffer_size=prefetch_buffer)

    return ds


def create_masks(tar):
    # Used in the 1st attention block in the decoder.
    # It is used to pad and mask future tokens in the input received by
    # the decoder.
    look_ahead_mask = transformer.create_look_ahead_mask(tf.shape(tar)[1])
    dec_target_padding_mask = transformer.create_padding_mask(tar)
    combined_mask = tf.maximum(dec_target_padding_mask, look_ahead_mask)

    return combined_mask


def train(train_data: Path, vocab_dir: Path, batch_size: int, shuffle_buffer: int, prefetch_buffer: int,
          num_layers: int, d_model: int, num_heads: int, dff: int, dropout_rate: 0.1, learning_rate: float,
          checkpoint_path: Path):
    # Training data
    train_ds = get_dataset(train_data, batch_size, shuffle_buffer, prefetch_buffer)
    vocab_size = get_vocab(vocab_dir).vocab_size + 2  # TODO: Add abstraction for the two special tokens?

    # Model
    transformer_decoder = transformer.TransformerOnlyDecoder(num_layers, d_model, num_heads, dff,
                                                             vocab_size, dropout_rate)

    # Loss
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True, reduction='none')

    def loss_function(real, pred):
        # Masks padded tokens from loss_object
        mask = tf.math.logical_not(tf.math.equal(real, 0))
        loss_ = loss_object(real, pred)

        mask = tf.cast(mask, dtype=loss_.dtype)
        loss_ *= mask

        return tf.reduce_mean(loss_)

    # Metrics
    train_loss = tf.keras.metrics.Mean(name='train_loss')
    train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')

    # Optimizer
    optimizer = tf.keras.optimizers.Adam(learning_rate, beta_1=0.9, beta_2=0.98, epsilon=1e-9)

    # Checkpointing
    ckpt = tf.train.Checkpoint(transformer_decoder=transformer_decoder, optimizer=optimizer)
    ckpt_manager = tf.train.CheckpointManager(ckpt, str(checkpoint_path), max_to_keep=5)
    if ckpt_manager.latest_checkpoint:
        ckpt.restore(ckpt_manager.latest_checkpoint)
        print('Latest checkpoint restored')

    # Tensorboard events
    train_log_dir = str(checkpoint_path / "events")
    train_summary_writer = tf.summary.create_file_writer(train_log_dir)

    @tf.function
    def train_step(tar):
        tar_inp = tar[:, :-1]
        tar_real = tar[:, 1:]

        mask = create_masks(tar_inp)

        with tf.GradientTape() as tape:
            predictions, _ = transformer_decoder(tar_inp, True, mask)
            loss = loss_function(tar_real, predictions)

        gradients = tape.gradient(loss, transformer_decoder.trainable_variables)
        optimizer.apply_gradients(zip(gradients, transformer_decoder.trainable_variables))

        train_loss(loss)
        train_accuracy(tar_real, predictions)

    try:
        while True:
            epoch_start = time.time()

            # Reset metrics
            train_loss.reset_states()
            train_accuracy.reset_states()

            for step, batch in enumerate(train_ds):
                train_step(batch)

                # Print intermediate metrics
                if step % 10 == 0:
                    print('Step {} Loss {:.4f} Accuracy {:.4f}'.format(
                        step + 1, train_loss.result(), train_accuracy.result()))
                    with train_summary_writer.as_default():
                        tf.summary.scalar('loss', train_loss.result(), step=step)
                        tf.summary.scalar('accuracy', train_accuracy.result(), step=step)

            print("Epoch finished in {} secs".format(time.time() - epoch_start))

            ckpt_save_path = ckpt_manager.save()
            print("Saving checkpoint at '{}'".format(ckpt_save_path))


    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser("Train")

    # Data params
    parser.add_argument("--train-data", type=Path, required=True)
    parser.add_argument("--vocab-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--shuffle-buffer", type=int, default=100)
    parser.add_argument("--prefetch-buffer", type=int, default=1)

    # Model params
    parser.add_argument("--num-layers", default=HPARAMS["num_layers"], type=int)
    parser.add_argument("--d-model", default=HPARAMS["d_model"], type=int)
    parser.add_argument("--num_heads", default=HPARAMS["num_heads"], type=int)
    parser.add_argument("--dff", default=HPARAMS["dff"], type=int)
    parser.add_argument("--dropout_rate", default=HPARAMS["dropout_rate"], type=int)
    parser.add_argument("--learning-rate", default=HPARAMS["learning_rate"], type=float)

    # Training params
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    # TODO: Add params for learning rate schedule?

    params = parser.parse_args()

    train(params.train_data, params.vocab_dir, params.batch_size, params.shuffle_buffer, params.prefetch_buffer,
          params.num_layers, params.d_model, params.num_heads, params.dff, params.dropout_rate, params.learning_rate,
          params.checkpoint_path)
