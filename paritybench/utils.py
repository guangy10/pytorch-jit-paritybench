import copy
import functools
import logging
import numpy as np
import os
import platform
import random
import re
import resource
import signal
import sys
import tempfile
import time
import types
import torch

from torch import multiprocessing
from torch._dynamo.utils import clone_inputs
from torch.utils._pytree import tree_map

from paritybench.reporting import ErrorAggregatorDict, Stats

log = logging.getLogger(__name__)


def call_with_timeout(fn, args, kwargs=None, timeout=10):
    kwargs = kwargs or {}
    parent_conn, child_conn = multiprocessing.Pipe()
    start = time.time()
    proc = multiprocessing.Process(target=call_with_timeout_subproc, args=(fn, args, kwargs, child_conn))
    proc.start()
    while proc.is_alive():
        if parent_conn.poll(1):
            result = parent_conn.recv()
            proc.join()
            return result
        if time.time() - start > timeout:
            os.kill(proc.pid, signal.SIGINT)  # maybe generate a stack trace for debugging
            time.sleep(1)
            proc.terminate()
            proc.join(10)
            raise TimeoutError(f"took longer than {timeout} seconds")

    proc.join()
    if proc.exitcode == 0:
        return parent_conn.recv()
    else:
        raise OSError(f"exitcode should be 0, got {proc.exitcode}")


def call_with_timeout_subproc(fn, args, kwargs, return_pipe):
    use_rlimit = (
        os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1024 ** 3 < 1000
        if platform.system() == "Linux"
        else True
    )
    if use_rlimit:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (int(os.environ.get("RLIMIT_AS_GB", 10)) * 1024 ** 3, hard))
    try:
        result = fn(*args, *kwargs)
        return_pipe.send(result)
    except Exception:
        log.exception("Error from subprocess")
        sys.exit(1)


def import_file(path):
    """
    :param path: to a *.py file
    :return: a python module
    """
    module = types.ModuleType(re.findall(r"test_[^.]+", path)[0])
    sys.modules[module.__name__] = module
    exec(compile(open(path).read(), filename=path, mode='exec'),
         module.__dict__, module.__dict__)
    if not hasattr(module, "TESTCASES"):
        module.TESTCASES = []
    return module


def subproc_wrapper(path: str, fn: callable, timeout: int = 900):
    """
    A wrapper around call_with_timeout() adding a temp dir and error handling.

    :param path: path to code to test
    :param fn: function to run in subprocess
    :param timeout: seconds to wait
    :return: errors, stats
    """
    log.info(f"Running {path}")
    with tempfile.TemporaryDirectory(prefix="paritybench") as tempdir:
        try:
            return call_with_timeout(fn, (tempdir, path), {}, timeout=timeout)
        except TimeoutError:
            return ErrorAggregatorDict.single(
                "meta",
                TimeoutError("Timeout testing module"),
                path
            ), Stats({"timeout": 1})
        except OSError:
            return ErrorAggregatorDict.single(
                "meta",
                OSError("Crash testing module"),
                path
            ), Stats({"crash": 1})


def tempdir_wrapper(path: str, fn: callable):
    """ Non-forking version of subproc_wrapper """
    log.info(f"Running {path}")
    with tempfile.TemporaryDirectory(prefix="paritybench") as tempdir:
        return fn(tempdir, path)


def wrap_args(args, device="cuda"):
    device = torch.device(device)
    return [x.to(device) if isinstance(x, torch.Tensor) else x for x in copy.deepcopy(args)]


def wrap_kwargs(kwargs, device="cuda"):
    device = torch.device(device)
    wrapped_kwargs = {}
    for k, v in kwargs.items():
        if isinstance(v, torch.Tensor):
            wrapped_kwargs.update({k: v.clone().to(device)})
        else:
            wrapped_kwargs.update({k: copy.deepcopy(v)})
    return wrapped_kwargs


def get_skiplist(main_args):
    if main_args.compile_mode == 'export':
        return SKIP.get("export")
    else:
        return SKIP.get(main_args.backend)


def get_tol(main_args):
    if main_args.backend == 'inductor':
        return INDUCTOR_TOL
    else:
        return DYNAMO_TOL


@functools.lru_cache(None)
def patch_torch_manual_seed():
    """Make torch manual seed deterministic. Helps with accuracy testing."""

    def deterministic_torch_manual_seed(*args, **kwargs):
        from torch._C import default_generator

        seed = 1337
        import torch.cuda

        if not torch.cuda._is_in_bad_fork():
            torch.cuda.manual_seed_all(seed)
        return default_generator.manual_seed(seed)

    torch.manual_seed = deterministic_torch_manual_seed


def reset_rng_state():
    torch.manual_seed(1337)
    random.seed(1337)
    np.random.seed(1337)


def cast_to(dtype, model, inputs):
    # cast model and inputs to fp16
    if dtype == torch.float16:
        model = model.half()
    else:
        model = model.to(dtype)

    inputs = tree_map(
        lambda x: x.to(dtype)
        if isinstance(x, torch.Tensor) and x.is_floating_point()
        else x,
        inputs,
    )
    return model, inputs


def cast_to_fp64(model, inputs):
    return cast_to(torch.float64, model, inputs)


def get_cosine_and_fp64_outputs(model, example_inputs):
    # Collect the fp64 reference outputs to be used later for accuracy checking.
    fp64_outputs = None
    cosine = False
    reset_rng_state()
    try:
        model_fp64, inputs_fp64 = cast_to_fp64(
            copy.deepcopy(model),
            clone_inputs(example_inputs),
        )
        fp64_outputs = model_fp64(inputs_fp64)
    except Exception:
        log.warning(
            "fp64 golden ref were not generated. Setting accuracy check to cosine",
        )
        cosine = True
        fp64_outputs = None
    return cosine, fp64_outputs


DYNAMO_TOL = 1e-4
INDUCTOR_TOL = 1e-3

SKIP_DYNAMO_EAGER = [
    "./generated/test_deepinsight_insightface.py:deeplab_xception_transfer_basemodel",
    "./generated/test_SforAiDl_vformer.py:PVTClassification",
    "./generated/test_SforAiDl_vformer.py:PVTDetection",  # try ... catch ...
    "./generated/test_BlinkDL_RWKV_LM.py:RWKV_ChannelMix",  # Subclasses torch.jit.ScriptModule
    "./generated/test_facebookresearch_pytext.py:ContextualTokenEmbedding",
    "./generated/test_pytorch_translate.py:OutputProjection",  # shape_as_tensor
    "./generated/test_ludwig_ai_ludwig.py:_VectorPreprocessing",
    "./generated/test_ludwig_ai_ludwig.py:_DatePreprocessing",  # torch.jit.isinstance
    "./generated/test_adapter_hub_adapter_transformers.py:GPTNeoSelfAttention", # torch.where with dtype torch.uint8 is now deprecated.
    "./generated/test_ZhaoJ9014_face_evoLVe.py:AM_Softmax",
    "./generated/test_ZhaoJ9014_face_evoLVe.py:CircleLoss",
    "./generated/test_ZhaoJ9014_face_evoLVe.py:MagFace",
    "./generated/test_fangchangma_self_supervised_depth_completion.py:PhotometricLoss",  # torch.index with dtype torch.uint8 is now deprecated.
]
SKIP_INDUCTOR = []
SKIP = {
    "eager": SKIP_DYNAMO_EAGER,
    "inductor": SKIP_DYNAMO_EAGER + SKIP_INDUCTOR,
    "export": SKIP_DYNAMO_EAGER,
}
