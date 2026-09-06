"""Build a clean, signed-off PR 2067 candidate on the pinned upstream tree.

This is fork-only remediation tooling, not part of the upstream patch.
Every replacement is counted; unexpected upstream drift fails closed.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

BASE = os.environ.get("PR2067_BASE", "8c76b723c723fd79163a6cca98f62d5af03fb722")
TARGET = Path("candle-binding/src/ffi/embedding.rs")
TEST = Path("candle-binding/src/ffi/embedding_init_test.rs")
MAKE = Path("tools/make/rust.mk")


def git(*args, env=None, input=None):
    return subprocess.check_output(["git", *args], env=env, input=input, text=True).strip()


s = git("show", f"{BASE}:{TARGET}") + "\n"

def replace(old, new, count=1):
    global s
    actual = s.count(old)
    if actual != count:
        raise RuntimeError(f"expected {count} occurrences, got {actual}: {old[:100]!r}")
    s = s.replace(old, new)


replace("use std::sync::OnceLock;", "use std::sync::{MutexGuard, OnceLock};")
replace("pub(crate) static GLOBAL_MODEL_FACTORY: OnceLock<ModelFactory> = OnceLock::new();", '''pub(crate) static GLOBAL_MODEL_FACTORY: OnceLock<ModelFactory> = OnceLock::new();

/// Serialize every factory-owning initializer from availability check through
/// publication. OnceLock protects publication, but not the preceding model load:
/// without this guard a losing initializer can discard a fully loaded mmBERT.
/// Embedding inference only reads OnceLocks and never takes this startup lock.
static EMBEDDING_INIT_LOCK: Mutex<()> = Mutex::new(());

fn lock_embedding_init() -> Option<MutexGuard<'static, ()>> {
    match EMBEDDING_INIT_LOCK.lock() {
        Ok(guard) => Some(guard),
        Err(error) => {
            eprintln!("ERROR: Embedding initialization lock poisoned: {}", error);
            None
        }
    }
}''')

helper = '''use crate::model_architectures::embedding::MmBertEmbeddingModel;

/// mmBERT can be loaded after a different model has claimed the immutable factory.
static STANDALONE_MMBERT: OnceLock<(MmBertEmbeddingModel, MmTokenizer)> = OnceLock::new();

/// Prefer the already-registered factory model; never replace it on repeated init.
fn get_mmbert_refs() -> Option<(&'static MmBertEmbeddingModel, &'static MmTokenizer)> {
    if let Some(factory) = GLOBAL_MODEL_FACTORY.get() {
        if let (Some(model), Some(tokenizer)) =
            (factory.get_mmbert_model(), factory.get_mmbert_tokenizer())
        {
            return Some((model, tokenizer));
        }
    }
    STANDALONE_MMBERT.get().map(|(model, tokenizer)| (model, tokenizer))
}

/// The caller must hold the initialization guard across the check, load and set.
/// A failed model/tokenizer load publishes nothing, so a later call can retry.
fn init_mmbert_standalone(
    model_path: &str,
    use_cpu: bool,
    _init_guard: &MutexGuard<'_, ()>,
) -> bool {
    use candle_core::Device;

    if get_mmbert_refs().is_some() {
        return true;
    }
    let device = if use_cpu {
        Device::Cpu
    } else {
        Device::cuda_if_available(0).unwrap_or(Device::Cpu)
    };
    let model = match MmBertEmbeddingModel::load(model_path, &device) {
        Ok(model) => model,
        Err(error) => {
            eprintln!("ERROR: Failed to load standalone mmBERT model: {:?}", error);
            return false;
        }
    };
    let tokenizer_path = format!("{}/tokenizer.json", model_path);
    let tokenizer = match MmTokenizer::from_file(&tokenizer_path) {
        Ok(tokenizer) => tokenizer,
        Err(error) => {
            eprintln!("ERROR: Failed to load standalone mmBERT tokenizer: {:?}", error);
            return false;
        }
    };
    match STANDALONE_MMBERT.set((model, tokenizer)) {
        Ok(()) => {
            println!("INFO: mmBERT embedding model registered in standalone storage");
            true
        }
        Err(_) => {
            eprintln!("ERROR: Standalone mmBERT storage changed while initialization was locked");
            false
        }
    }
}

'''
replace("/// Generic internal helper for single text embedding generation", helper + "/// Generic internal helper for single text embedding generation")

# Apply the guard before any availability check or model loading in all four
# owners of GLOBAL_MODEL_FACTORY. The named guard remains alive to function exit.
initializers = ["init_mmbert_embedding_model", "init_embedding_models_with_mmbert", "init_embedding_models", "init_multimodal_embedding_model"]
for name in initializers:
    pattern = r'(pub extern "C" fn ' + name + r'\([^)]*\) -> bool \{\n    use candle_core::Device;\n)'
    guard = "init_guard" if name in initializers[:2] else "_init_guard"
    addition = f"\n    let Some({guard}) = lock_embedding_init() else {{\n        return false;\n    }};\n"
    s, n = re.subn(pattern, lambda m: m.group(1) + addition, s)
    if n != 1:
        raise RuntimeError(f"could not locate initializer {name}: {n}")

replace('''    // Check if already initialized
    if let Some(factory) = GLOBAL_MODEL_FACTORY.get() {
        if factory.get_mmbert_model().is_some() {
            eprintln!("WARNING: mmBERT model already initialized");
            return true;
        }
    }''', '''    // Repeated initialization must not reload or switch model path/device.
    if get_mmbert_refs().is_some() {
        return true;
    }''')
replace('''        // Factory exists but mmbert not loaded - we can't modify OnceLock
        eprintln!("Error: ModelFactory already initialized without mmBERT. Initialize mmBERT first or use init_embedding_models_with_mmbert.");
        return false;''', '''        // Another model owns the immutable factory. Keep mmBERT reachable
        // through standalone storage without replacing that factory.
        return init_mmbert_standalone(&path, use_cpu, &init_guard);''')

# Only the combined initializer has this exact early-return block.
replace('''    if GLOBAL_MODEL_FACTORY.get().is_some() {
        eprintln!("WARNING: ModelFactory already initialized");
        return true;
    }''', '''    if GLOBAL_MODEL_FACTORY.get().is_some() {
        // Preserve the loaded-model fast path before parsing any replacement path.
        if get_mmbert_refs().is_some() || mmbert_model_path.is_null() {
            return true;
        }
        let path = unsafe {
            match CStr::from_ptr(mmbert_model_path).to_str() {
                Ok(path) if !path.is_empty() => path,
                _ => return true, // No optional mmBERT path, as on first init.
            }
        };
        return init_mmbert_standalone(path, use_cpu, &init_guard);
    }''')
replace('''        Err(_) => true, // Already initialized''', '''        Err(_) => {
            eprintln!("ERROR: ModelFactory changed while embedding initialization was locked");
            false
        }''')
replace('''            // Already initialized - idempotent behavior
            true''', '''            eprintln!("ERROR: ModelFactory changed while embedding initialization was locked");
            false''')

# Shared mmBERT resolution for both explicit and automatic embedding dispatch.
for name in ["generate_mmbert_embedding", "generate_mmbert_embeddings_batch"]:
    replace(f"fn {name}(\n    factory: &ModelFactory,", f"fn {name}(\n    _factory: &ModelFactory,")
replace('''    let model = factory
        .get_mmbert_model()
        .ok_or_else(|| "mmBERT model not available".to_string())?;

    let tokenizer = factory
        .get_mmbert_tokenizer()
        .ok_or_else(|| "mmBERT tokenizer not available".to_string())?;''', '''    let (model, tokenizer) =
        get_mmbert_refs().ok_or_else(|| "mmBERT model not available".to_string())?;''', count=2)
old_checks = ["factory.map_or(false, |f| f.get_mmbert_model().is_some())", "factory.is_some_and(|f| f.get_mmbert_model().is_some())"]
count = sum(s.count(check) for check in old_checks)
if count != 3:
    raise RuntimeError(f"expected three automatic mmBERT dispatch checks, found {count}")
for check in old_checks:
    s = s.replace(check, "get_mmbert_refs().is_some()")
if s.count("GLOBAL_MODEL_FACTORY.set(") != 4:
    raise RuntimeError("unexpected additional factory writer: review its locking before continuing")
s += '\n#[cfg(test)]\n#[path = "embedding_init_test.rs"]\nmod init_order_tests;\n'
TARGET.write_text(s)
TEST.write_text(Path("tools/pr2067/embedding_init_test.rs").read_text())
make = git("show", f"{BASE}:{MAKE}") + "\n"
anchor = "RUST_CI_LIB_TESTS ?= " + chr(92) + "\n"
if make.count(anchor) != 1:
    raise RuntimeError("could not locate Rust CI allowlist")
MAKE.write_text(make.replace(anchor, anchor + "\tffi::embedding::init_order_tests::embedding_init_order_regressions " + chr(92) + "\n"))
subprocess.run(["rustfmt", "--edition", "2021", "--config", "skip_children=true", str(TARGET), str(TEST)], check=True)
subprocess.run(["git", "diff", "--check"], check=True)

# Build a tree on upstream, excluding all fork-only workflow/preparation files.
with tempfile.TemporaryDirectory() as directory:
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(Path(directory) / "index")
    git("read-tree", BASE, env=env)
    git("add", "--", str(TARGET), str(TEST), str(MAKE), env=env)
    tree = git("write-tree", env=env)
    env.update(GIT_AUTHOR_NAME="Josh Jones", GIT_AUTHOR_EMAIL="262070388+scoobydont-666@users.noreply.github.com",
               GIT_COMMITTER_NAME="Josh Jones", GIT_COMMITTER_EMAIL="262070388+scoobydont-666@users.noreply.github.com")
    message = """[Router] fix mmBERT initialization ordering, concurrency and idempotency

Reapply PR #2067 to current main. Serialize all factory-owning initializers
across availability checks, model loading and publication. Resolve mmBERT
from the existing factory or standalone storage without reloading a ready
model. Keep inference lock-free and initialization failures retryable.

Add isolated-process CPU regressions with tiny locally generated mmBERT
checkpoints for both ownership orders, competing initializers, all four
entrypoint gates, repeated/replacement paths, failed-load retry and poison.
Run the new regression in the existing model-free Rust CI allowlist.

Signed-off-by: Josh Jones <262070388+scoobydont-666@users.noreply.github.com>
"""
    candidate = git("commit-tree", tree, "-p", BASE, env=env, input=message)
changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", candidate).splitlines()
if sorted(changed) != sorted([str(TARGET), str(TEST), str(MAKE)]):
    raise RuntimeError(f"unexpected candidate paths: {changed}")
branch = f"work/pr-2067-candidate-{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
git("push", "origin", f"{candidate}:refs/heads/{branch}")
Path("/tmp/pr2067-candidate.sha").write_text(candidate + "\n")
# Test a clean checkout of this exact candidate, not an uncommitted workspace.
git("reset", "--hard", candidate)
print(f"PR2067_CANDIDATE_SHA={candidate}", flush=True)
print(git("show", "--stat", "--oneline", "HEAD"), flush=True)
