# Vendored from https://github.com/shiyu-coder/Kronos (MIT, see LICENSE).
# Patches: relative import; auto_regressive_inference(return_samples=True) keeps all sampled paths.
from .kronos import KronosTokenizer, Kronos, KronosPredictor, auto_regressive_inference, calc_time_stamps

