'''Train a multi-task model using AllenNLP

To debug this, run with -m ipdb:

    python -m ipdb main.py --config_file ...
'''
# pylint: disable=no-member
import argparse
import glob
import os
import subprocess
import random
import sys
import time

import logging as log
log.basicConfig(format='%(asctime)s: %(message)s',
                datefmt='%m/%d %I:%M:%S %p', level=log.INFO)

import torch

from src import config
from src import gcp

from src.utils import assert_for_log, maybe_make_dir, load_model_state
from src.preprocess import build_tasks
from src.tasks import NLITypeProbingTask
from src import evaluate


def handle_arguments(cl_arguments):
    parser = argparse.ArgumentParser(description='')
    # Configuration files
    parser.add_argument('--config_file', '-c', type=str, nargs="+",
                        help="Config file(s) (.conf) for model parameters.")
    parser.add_argument('--overrides', '-o', type=str, default=None,
                        help="Parameter overrides, as valid HOCON string.")

    parser.add_argument('--remote_log', '-r', action="store_true",
                        help="If true, enable remote logging on GCP.")

    parser.add_argument('--notify', type=str, default="",
                        help="Email address for job notifications.")

    parser.add_argument('--tensorboard', '-t', action="store_true",
                        help="If true, will run Tensorboard server in a "
                        "subprocess, serving on the port given by "
                        "--tensorboard_port.")
    parser.add_argument('--tensorboard_port', type=int, default=6006)

    return parser.parse_args(cl_arguments)


def _try_logging_git_info():
    try:
        log.info("Waiting on git info....")
        c = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           timeout=10, stdout=subprocess.PIPE)
        git_branch_name = c.stdout.decode().strip()
        log.info("Git branch: %s", git_branch_name)
        c = subprocess.run(["git", "rev-parse", "HEAD"],
                           timeout=10, stdout=subprocess.PIPE)
        git_sha = c.stdout.decode().strip()
        log.info("Git SHA: %s", git_sha)
    except subprocess.TimeoutExpired as e:
        log.exception(e)
        log.warn("Git info not found. Moving right along...")


def _run_background_tensorboard(logdir, port):
    """Run a TensorBoard server in the background."""
    import atexit
    tb_args = ["tensorboard", "--logdir", logdir,
               "--port", str(port)]
    log.info("Starting TensorBoard server on port %d ...", port)
    tb_process = subprocess.Popen(tb_args)
    log.info("TensorBoard process: %d", tb_process.pid)

    def _kill_tb_child():
        log.info("Shutting down TensorBoard server on port %d ...", port)
        tb_process.terminate()
    atexit.register(_kill_tb_child)

# Global notification handler, can be accessed outside main() during exception
# handling.
EMAIL_NOTIFIER = None

def main(cl_arguments):
    ''' Train or load a model. Evaluate on some tasks. '''
    cl_args = handle_arguments(cl_arguments)
    args = config.params_from_file(cl_args.config_file, cl_args.overrides)

    # Logistics #
    maybe_make_dir(args.project_dir)  # e.g. /nfs/jsalt/exp/$HOSTNAME
    maybe_make_dir(args.exp_dir)      # e.g. <project_dir>/jiant-demo
    maybe_make_dir(args.run_dir)      # e.g. <project_dir>/jiant-demo/sst
    log.getLogger().addHandler(log.FileHandler(args.local_log_path))

    if cl_args.remote_log:
        gcp.configure_remote_logging(args.remote_log_name)

    if cl_args.notify:
        from src import emails
        global EMAIL_NOTIFIER
        log.info("Registering email notifier for %s", cl_args.notify)
        EMAIL_NOTIFIER = emails.get_notifier(cl_args.notify, args)

    if EMAIL_NOTIFIER:
        EMAIL_NOTIFIER(body="Starting run.", prefix="")

    _try_logging_git_info()

    log.info("Parsed args: \n%s", args)

    config_file = os.path.join(args.run_dir, "params.conf")
    config.write_params(args, config_file)
    log.info("Saved config to %s", config_file)

    seed = random.randint(1, 10000) if args.random_seed < 0 else args.random_seed
    random.seed(seed)
    torch.manual_seed(seed)
    log.info("Using random seed %d", seed)
    if args.cuda >= 0:
        try:
            if not torch.cuda.is_available():
                raise EnvironmentError("CUDA is not available, or not detected"
                                       " by PyTorch.")
            log.info("Using GPU %d", args.cuda)
            torch.cuda.set_device(args.cuda)
            torch.cuda.manual_seed_all(seed)
        except Exception:
            log.warning(
                "GPU access failed. You might be using a CPU-only installation of PyTorch. Falling back to CPU.")
            args.cuda = -1

    # Prepare data #
    log.info("Loading tasks...")
    start_time = time.time()
    train_tasks, eval_tasks, vocab, word_embs = build_tasks(args)
    if any([t.val_metric_decreases for t in train_tasks]) and any([not t.val_metric_decreases for t in train_tasks]):
        log.warn("\tMixing training tasks with increasing and decreasing val metrics!")
    tasks = sorted(set(train_tasks + eval_tasks), key=lambda x: x.name)
    log.info('\tFinished loading tasks in %.3fs', time.time() - start_time)
    log.info('\t Tasks: {}'.format([task.name for task in tasks]))

    if args.metatrain:
        log.info("\tDOING METATRAINING!")
        from src.metamodels import build_model
        from src.metatrainer import build_trainer, build_trainer_params
    else:
        from src.models import build_model
        from src.trainer import build_trainer, build_trainer_params

    # Build or load model #
    log.info('Building model...')
    start_time = time.time()
    model = build_model(args, vocab, word_embs, tasks)
    log.info('\tFinished building model in %.3fs', time.time() - start_time)

    # Check that necessary parameters are set for each step. Exit with error if not.
    steps_log = []

    if not args.load_eval_checkpoint == 'none':
        assert_for_log(os.path.exists(args.load_eval_checkpoint),
                       "Error: Attempting to load model from non-existent path: [%s]" %
                       args.load_eval_checkpoint)
        assert_for_log(not args.do_train,
                       "Error: Attempting to train a model and then replace that model with one from a checkpoint.")
        steps_log.append("Loading model from path: %s" % args.load_eval_checkpoint)

    if args.do_train:
        assert_for_log(args.train_tasks != "none",
                       "Error: Must specify at least on training task: [%s]" % args.train_tasks)
        assert_for_log(args.val_interval % args.bpp_base == 0,
                       "Error: val_interval [%d] must be divisible by bpp_base [%d]" % (args.val_interval,args.bpp_base))
        steps_log.append("Training model on tasks: %s" % args.train_tasks)

    if args.train_for_eval:
        steps_log.append("Re-training model for individual eval tasks")
        assert_for_log(args.eval_val_interval % args.bpp_base == 0,
                       "Error: eval_val_interval [%d] must be divisible by bpp_base [%d]" % (args.eval_val_interval,args.bpp_base))
        assert_for_log(len(set(train_tasks).intersection(eval_tasks)) == 0 \
                        or args.allow_reuse_of_pretraining_parameters \
                        or args.do_train == 0,
                        "If you're pretraining on a task you plan to reuse as a target task, set\n"
                        "allow_reuse_of_pretraining_parameters = 1 (risky), or train in two steps:\n"
                        "  train with do_train = 1, train_for_eval = 0, stop, and restart with\n"
                        "  do_train = 0 and train_for_eval = 1.")

    if args.do_eval:
        assert_for_log(args.eval_tasks != "none",
                       "Error: Must specify at least one eval task: [%s]" % args.eval_tasks)
        steps_log.append("Evaluating model on tasks: %s" % args.eval_tasks)

    # Start Tensorboard if requested
    if cl_args.tensorboard:
        tb_logdir = os.path.join(args.run_dir, "tensorboard")
        _run_background_tensorboard(tb_logdir, cl_args.tensorboard_port)
    log.info("Will run the following steps:\n%s", '\n'.join(steps_log))

    if args.do_train:
        # Train on train tasks #
        log.info("Training...")
        params = build_trainer_params(args, task_names=[])
        stop_metric = train_tasks[0].val_metric if len(train_tasks) == 1 else 'macro_avg'
        should_decrease = train_tasks[0].val_metric_decreases if len(train_tasks) == 1 else False
        trainer, _, opt_params, schd_params = build_trainer(params, model,
                                                            args.run_dir,
                                                            should_decrease)
        to_train = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
        best_epochs = trainer.train(train_tasks, stop_metric,
                                    args.batch_size, args.bpp_base,
                                    args.weighting_method, args.scaling_method,
                                    to_train, opt_params, schd_params,
                                    args.shared_optimizer, args.load_model, phase="main")

    # Select model checkpoint from main training run to load
    if not args.train_for_eval:
        log.info("In strict mode because train_for_eval is off. "
                 "Will crash if any tasks are missing from the checkpoint.")
        strict = True
    else:
        strict = False

    if args.train_for_eval and not args.allow_reuse_of_pretraining_parameters:
        # If we're training models for evaluation, which is always done from scratch with a fresh
        # optimizer, we shouldn't load parameters for those models.
        # Usually, there won't be trained parameters to skip, but this can happen if a run is killed
        # during the train_for_eval phase.
        task_names_to_avoid_loading = [task.name for task in eval_tasks]
    else:
        task_names_to_avoid_loading = []

    if not args.load_eval_checkpoint == "none":
        log.info("Loading existing model from %s...", args.load_eval_checkpoint)
        load_model_state(model, args.load_eval_checkpoint,
                         args.cuda, task_names_to_avoid_loading, strict=strict)
    else:
        # Look for eval checkpoints (available only if we're restoring from a run that already
        # finished), then look for training checkpoints.
        eval_best = glob.glob(os.path.join(args.run_dir,
                                           "model_state_eval_best.th"))
        if len(eval_best) > 0:
            load_model_state(model, eval_best[0], args.cuda, task_names_to_avoid_loading, strict=strict)
        else:
            macro_best = glob.glob(os.path.join(args.run_dir,
                                                "model_state_main_epoch_*.best_macro.th"))
            if len(macro_best) > 0:
                assert_for_log(len(macro_best) == 1, "Too many best checkpoints. Something is wrong.")
                load_model_state(model, macro_best[0], args.cuda, task_names_to_avoid_loading, strict=strict)
            else:
                assert_for_log(
                    args.allow_untrained_encoder_parameters,
                    "No best checkpoint found to evaluate.")
                log.warning("Evaluating untrained encoder parameters!")

    # Train just the task-specific components for eval tasks.
    if args.train_for_eval:
        # might be empty if no elmo. scalar_mix_0 should always be pretrain scalars
        elmo_scalars = [(n, p) for n, p in model.named_parameters() if
                        "scalar_mix" in n and "scalar_mix_0" not in n]
        # fails when sep_embs_for_skip is 0 and elmo_scalars has nonzero length
        assert_for_log(not elmo_scalars or args.sep_embs_for_skip,
                       "Error: ELMo scalars loaded and will be updated in train_for_eval but "
                       "they should not be updated! Check sep_embs_for_skip flag or make an issue.")
        for task in eval_tasks:
            # Skip mnli-diagnostic
            # This has to be handled differently than probing tasks because probing tasks require the "is_probing_task"
            # to be set to True. For mnli-diagnostic this flag will be False because it is part of GLUE and
            # "is_probing_task is global flag specific to a run, not to a task.
            if task.name == 'mnli-diagnostic':
                continue
            pred_module = getattr(model, "%s_mdl" % task.name)
            to_train = elmo_scalars + [(n, p) for n, p in pred_module.named_parameters() if p.requires_grad]
            # Look for <task_name>_<param_name>, then eval_<param_name>
            params = build_trainer_params(args, task_names=[task.name, 'eval'])
            trainer, _, opt_params, schd_params = build_trainer(params, model,
                                                                args.run_dir,
                                                                task.val_metric_decreases)
            best_epoch = trainer.train([task], task.val_metric,
                                       args.batch_size, 1,
                                       args.weighting_method, args.scaling_method,
                                       to_train, opt_params, schd_params,
                                       args.shared_optimizer, load_model=False, phase="eval")

            # Now that we've trained a model, revert to the normal checkpoint logic for this task.
            if task.name in task_names_to_avoid_loading:
                task_names_to_avoid_loading.remove(task.name)

            # The best checkpoint will accumulate the best parameters for each task.
            # This logic looks strange. We think it works.
            best_epoch = best_epoch[task.name]
            layer_path = os.path.join(args.run_dir, "model_state_eval_best.th")
            load_model_state(model, layer_path, args.cuda, skip_task_models=task_names_to_avoid_loading, strict=strict)

    if args.do_eval:
        # Evaluate #
        log.info("Evaluating...")
        val_results, val_preds = evaluate.evaluate(model, eval_tasks,
                                                   args.batch_size,
                                                   args.cuda, "val")

        splits_to_write = evaluate.parse_write_preds_arg(args.write_preds)
        if 'val' in splits_to_write:
            evaluate.write_preds(eval_tasks, val_preds, args.run_dir, 'val',
                                 strict_glue_format=args.write_strict_glue_format)
        if 'test' in splits_to_write:
            _, te_preds = evaluate.evaluate(model, eval_tasks,
                                            args.batch_size, args.cuda, "test")
            evaluate.write_preds(tasks, te_preds, args.run_dir, 'test',
                                 strict_glue_format=args.write_strict_glue_format)
        run_name = args.get("run_name", os.path.basename(args.run_dir))

        results_tsv = os.path.join(args.exp_dir, "results.tsv")
        log.info("Writing results for split 'val' to %s", results_tsv)
        evaluate.write_results(val_results, results_tsv, run_name=run_name)

    log.info("Done!")


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
        if EMAIL_NOTIFIER is not None:
            EMAIL_NOTIFIER(body="Run completed successfully!", prefix="")
    except BaseException as e:
        # Make sure we log the trace for any crashes before exiting.
        log.exception("Fatal error in main():")
        if EMAIL_NOTIFIER is not None:
            import traceback
            tb_lines = traceback.format_exception(*sys.exc_info())
            EMAIL_NOTIFIER(body="".join(tb_lines), prefix="FAILED")
        raise e  # re-raise exception, in case debugger is attached.
        sys.exit(1)
    sys.exit(0)
