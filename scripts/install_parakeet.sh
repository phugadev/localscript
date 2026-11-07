#!/usr/bin/env bash
# Build transcribe.cpp (https://github.com/handy-computer/transcribe.cpp, MIT) so LocalScript can run
# Parakeet Ultra. Metal is enabled automatically on Apple Silicon. Safe to re-run.
#
# Result: $LOCALSCRIPT_HOME/engines/transcribe.cpp/build-shared/src/libtranscribe.dylib
#         (LocalScript loads it through the Python binding that lives in the same checkout)
#
# The Parakeet Ultra model itself (740 MB GGUF) downloads from Hugging Face the first time it is used,
# unless you already have it (the Handy app keeps the same file in the Hugging Face cache).
set -euo pipefail

PIN="5700f127e38dd53c4735ea23ef743b4b538eacd4"   # commit this was developed and tested against
ENGINES="${LOCALSCRIPT_HOME:-$HOME/.localscript}/engines"
REPO="$ENGINES/transcribe.cpp"

for tool in git cmake clang++; do
  command -v "$tool" >/dev/null || { echo "Missing '$tool' (brew install cmake; xcode-select --install)"; exit 1; }
done

mkdir -p "$ENGINES"
if [ ! -d "$REPO/.git" ]; then
  git clone https://github.com/handy-computer/transcribe.cpp.git "$REPO"
fi
git -C "$REPO" fetch --quiet origin || true
git -C "$REPO" checkout --quiet "$PIN"

# Some Command Line Tools installs are incomplete: clang can't find the C++ standard library headers
# (error: 'array' file not found) even though the SDK ships them. If a probe compile fails, point clang
# at the SDK's copy for this build only. Nothing on the system is modified.
CMAKE_EXTRA=()
probe() { printf '#include <array>\nint main(){}\n' | clang++ -x c++ -std=c++17 "$@" - -o /dev/null 2>/dev/null; }
if ! probe; then
  SDK="$(xcrun --show-sdk-path 2>/dev/null || true)"
  if [ -n "$SDK" ] && probe -stdlib++-isystem "$SDK/usr/include/c++/v1"; then
    echo "Note: Command Line Tools can't find libc++ headers; using the SDK copy for this build."
    FLAGS="-stdlib++-isystem $SDK/usr/include/c++/v1"
    CMAKE_EXTRA=(-DCMAKE_CXX_FLAGS="$FLAGS" -DCMAKE_OBJCXX_FLAGS="$FLAGS")
  else
    echo "clang++ can't compile a trivial C++ file. Reinstall the Command Line Tools, then re-run this."
    exit 1
  fi
fi

cmake -S "$REPO" -B "$REPO/build-shared" -DCMAKE_BUILD_TYPE=Release \
  -DTRANSCRIBE_BUILD_SHARED=ON -DTRANSCRIBE_BUILD_TESTS=OFF -DTRANSCRIBE_BUILD_EXAMPLES=OFF \
  ${CMAKE_EXTRA[@]+"${CMAKE_EXTRA[@]}"}
cmake --build "$REPO/build-shared" --config Release -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

echo
echo "Parakeet engine built: $REPO/build-shared"
echo "Restart LocalScript; 'auto' will now use Parakeet for European languages."
