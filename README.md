# MoneyPrinterTurbo Offline-Hardened Fork

This is an English-first fork of
[MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo). It keeps
the upstream application available for reference while adding a narrowly
scoped, security-hardened workflow for editing media you already own or are
authorized to use.

> [!IMPORTANT]
> The hardened boundary applies only to `Dockerfile.offline` and
> `compose.offline.yaml`. The complete upstream application still contains
> networked AI, stock-media, speech, and publishing integrations and is not
> equivalently isolated.

## What the approved offline workflow does

The offline editor accepts one to twenty JPG or PNG files and produces a
deterministic portrait MP4. It can:

- scale and crop supplied images;
- apply restrained motion and fades;
- add up to three lines of operator-approved text;
- create an audit manifest containing source and output hashes.

It does **not**:

- create AI-generated video or photographs;
- download stock media;
- call an LLM or text-to-speech service;
- generate music;
- upload or publish the result.

Every source image, text claim, and finished edit still requires human review.

## Security controls

The `offline-pilot` container runs with:

- no network namespace;
- a read-only container filesystem;
- read-only input media;
- one writable output directory;
- an unprivileged user (`65532:65532`);
- all Linux capabilities removed;
- `no-new-privileges` enabled;
- CPU, memory, process, and temporary-storage limits.

The image contains only the four application modules needed for the offline
editor. It does not contain the WebUI, API server, provider integrations,
credentials, or supplied media.

## Requirements

- Docker Engine with Docker Compose v2
- Python 3.11 or later for the build-metadata command
- one to twenty approved JPG or PNG files
- enough disk space for the source snapshots and rendered output

Each image may be at most 50 MiB. Total input may be at most 200 MiB. Images
may not exceed 8192 by 8192 pixels or 33,554,432 pixels.

## Quick start

Run the following commands from the repository root:

```shell
mkdir -p offline-input offline-output/tmp
sudo chown -R 65532:65532 offline-output
sudo chmod 0750 offline-output offline-output/tmp
```

Copy only approved source images into `offline-input`, then create
`offline-input/plan.json`:

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

Prepare the build metadata:

```shell
export OFFLINE_INPUT_DIR="$PWD/offline-input"
export OFFLINE_OUTPUT_DIR="$PWD/offline-output"
export OFFLINE_APP_COMMIT="$(git rev-parse HEAD)"
export OFFLINE_APP_VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
```

Build and run the isolated editor:

```shell
docker compose -f compose.offline.yaml build

docker compose -f compose.offline.yaml run --rm offline-pilot \
  --input-root /input \
  --output-root /output \
  --plan plan.json \
  photo-1.jpg photo-2.jpg
```

The default outputs are:

- `offline-output/<slug>.mp4`
- `offline-output/<slug>.manifest.json`

The command fails closed if the required directories are missing, the input
mount is writable, an input is a symlink or changes during inspection, a file
exceeds its limit, or the requested output already exists.

## Before using an edit publicly

Confirm all of the following:

1. Every visible product and setting came from approved source files.
2. Every text claim is factual and supportable.
3. Editing has not materially changed the product's color, size, condition, or
   performance.
4. The MP4 hash matches the manifest.
5. A human approved the exact final file.

## Documentation

- [Complete offline-pilot guide](docs/offline-pilot.md)
- [Full English documentation for the upstream application](README-en.md)
- [Original upstream repository](https://github.com/harry0703/MoneyPrinterTurbo)
- [Issues for this fork](https://github.com/wcfcarolina13/MoneyPrinterTurbo-offline-hardened/issues)

## Update policy

Upstream changes are not merged automatically. Any upstream update should be
reviewed and re-tested against the offline security boundary before it enters
this fork.

## License

See [`LICENSE`](LICENSE). This fork preserves the upstream project's licensing
and attribution.
