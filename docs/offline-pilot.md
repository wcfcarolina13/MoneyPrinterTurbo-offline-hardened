# Hardened offline editing pilot

This fork includes a narrow editing path for turning caller-supplied JPG and
PNG files into a deterministic portrait MP4. The pilot is deliberately
separate from MoneyPrinterTurbo's networked generation workflow.

The pilot does not:

- generate video or images;
- download stock media;
- call an LLM or text-to-speech service;
- generate music;
- upload or publish the result.

It scales and crops the supplied images, applies restrained motion and fades,
adds up to three caller-approved text lines, and records what it did in a JSON
manifest. A person must still review the copy and final edit before marketing
use.

## Security boundary

The `offline-pilot` Compose service runs with:

- no network namespace;
- a read-only container filesystem;
- a read-only input bind mount;
- one writable output bind mount;
- an unprivileged UID and GID (`65532:65532`);
- all Linux capabilities dropped;
- `no-new-privileges` enabled;
- CPU, memory, process, and temporary-storage limits.

Only four application modules are copied into the image. The image does not
contain the WebUI, API server, provider integrations, credentials, or supplied
media. The base image is pinned by digest, and the application commit is
embedded in every output manifest.

This boundary applies only to `Dockerfile.offline` and
`compose.offline.yaml`. It is not a claim that the complete upstream
application is offline or equivalently hardened.

## Requirements

- Docker Engine with Docker Compose v2 (`docker compose`)
- a checkout of this fork
- one to twenty approved JPG or PNG files
- enough output space for the input snapshots and rendered files

Each image may be at most 50 MiB. The aggregate input limit is 200 MiB. Image
dimensions may not exceed 8192 by 8192 or 33,554,432 pixels. The JSON plan may
be at most 8 KiB.

## Prepare the input and output directories

Run these commands from the repository root:

```shell
mkdir -p offline-input offline-output/tmp
sudo chown -R 65532:65532 offline-output
sudo chmod 0750 offline-output offline-output/tmp
```

Copy only approved source images into `offline-input`. Keep the media and
rendered output uncommitted; both directories are ignored by Git.

Create `offline-input/plan.json`:

```json
{
  "slug": "sample-product-edit",
  "lines": [
    "Product name",
    "A factual, supportable description",
    "A truthful call to action"
  ]
}
```

The slug must contain only lowercase letters, digits, and hyphens and may be
at most 64 characters. Supply one to three trimmed, printable text lines of at
most 80 characters each. The renderer does not invent or verify claims; the
operator owns the wording.

## Build and run

Export absolute host paths and the source identity:

```shell
export OFFLINE_INPUT_DIR="$PWD/offline-input"
export OFFLINE_OUTPUT_DIR="$PWD/offline-output"
export OFFLINE_APP_COMMIT="$(git rev-parse HEAD)"
export OFFLINE_APP_VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
```

Build the isolated image:

```shell
docker compose -f compose.offline.yaml build
```

Render one or more root-level input files:

```shell
docker compose -f compose.offline.yaml run --rm offline-pilot \
  --input-root /input \
  --output-root /output \
  --plan plan.json \
  photo-1.jpg photo-2.jpg
```

The default output name is `<slug>.mp4`. To choose another root-level MP4
name, add `--output approved-name.mp4`.

The run fails closed if required directories are missing, the input mount is
writable, the output or its `tmp` directory is not writable by UID 65532, an
input is a symlink or changes during inspection, a file exceeds its limit, or
an output with the same name already exists.

## Review the evidence

A successful run writes two files:

- `<slug>.mp4`, the rendered edit;
- `<slug>.manifest.json`, a portable audit record.

The manifest contains source hashes and dimensions, the approved text, fixed
editing operations, output metadata and hash, FFmpeg identities, application
module hashes, and the embedded Git commit. It contains relative filenames,
not host filesystem paths.

Before any public use, verify:

1. Every visible product and setting came from the approved source files.
2. Text describes only facts the business can substantiate.
3. Editing has not materially altered product color, size, condition, or
   performance.
4. The MP4 hash matches the manifest.
5. A human has approved the exact final file.

The manifest intentionally reports every generation and publication feature
as `false`; publishing remains a separate human-controlled action.
