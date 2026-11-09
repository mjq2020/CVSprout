# CVSprout

**English** | [简体中文](README.zh-CN.md)

**Annotate, train, and deploy computer vision models on your own GPU.**

Run your computer vision workflow on your own hardware: import images → AI annotation and human review → dataset versioning → training → model conversion and inference deployment.

CVSprout supports object detection, instance segmentation, image classification, and keypoints. It is designed for personal computers, single-GPU servers, and small teams.

The interface supports **Simplified Chinese and English**, including dialogs, validation, progress messages, and API errors. Use the dedicated language selector at the top of the login screen or in the workspace sidebar. Your browser remembers the choice; project names, labels, and other user content retain their original language. See [Language settings and translation maintenance](docs/localization.md).

Each regular user has an independent workspace, and projects are private by default. Add collaborators or read-only members through **Project members and sharing**. System administrators can manage all projects. For account and project ownership migration during upgrades, see [Personal workspaces and project sharing](docs/workspaces.md).

Teams can use [work blocks](docs/user-guide.md#任务块协作): administrators split images into blocks, members claim blocks exclusively, and reviewers check the completed annotations. Fully approved blocks retain their completion records to prevent duplicate assignment.

The left sidebar brings together the current project, workflow, annotation service status, and account controls. The **Theme** control at the bottom switches between light, dark, and system settings, and the browser remembers your choice. On mobile, open navigation from the top menu. Page transitions keep existing content visible while loading, and data searches and loading more results preserve your position. The interface also respects the system's reduced-motion setting. See [Interface and themes](docs/user-guide.md#界面与主题).

[reCamera Pro integration](docs/recamera-pro.md) is supported: administrators create a connection under **Edge devices**, download and install **CVSprout Connector** (which retains the application ID `cvsprout-capture`), and enter the platform URL and device token. The device then automatically uploads original images to the project for the same work-block collaboration workflow. A single connector handles both image capture and model transfer.

After training a YOLOv8 / YOLO11 object detection model, select **RKNN → reCamera Pro system model** under **Training and conversion**, then publish and send the model from **Edge devices → Model deployment**. You can add it to the device's model library only, or explicitly confirm replacement of the active system inference model. The page reports model import and runtime verification separately. System models run through the device's built-in inference service, and the device polls for tasks, so no SSH connection from the server to the device is required. Compatible device firmware and authorization for the device's local model API are required. Existing standalone inference application packages and SSH deployment records remain available through the legacy interface.

Original code is licensed under [Apache-2.0](LICENSE), allowing free commercial use. Dependencies and models, including Ultralytics, have their own licensing requirements. Read the [Third-party notices](THIRD_PARTY_NOTICES.md) before using them in a closed-source commercial product.

## Quick start

Install Docker. NVIDIA GPU users also need working drivers and container GPU support:

| Environment | Requirements |
|---|---|
| Linux x86-64 | Docker Engine and Compose; GPU mode also requires NVIDIA Container Toolkit |
| Windows x86-64 | Docker Desktop with WSL2 enabled; GPU mode requires an NVIDIA driver with WSL2 support |
| CPU | Supports manual annotation and small training jobs; a GPU is recommended for large models |
| macOS / AMD GPU | Native acceleration has not been verified in this release; use a browser to access a GPU server |

You can currently start from a source checkout. Installer packages will also be available once official releases are published:

```bash
git clone https://github.com/mjq2020/CVSprout.git
cd CVSprout
```

Run from the project directory:

```bash
# Linux / WSL: detect NVIDIA on first launch, then reuse the last successful mode
./start.sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The initial default address is http://127.0.0.1:8000. If the port is occupied, the launcher automatically selects an available one. Open the actual URL printed by the launcher, copy the setup token, and create an administrator account in the browser. After a successful start, the actual port is saved as `MDP_PORT` in `.env` for future launches. `./start.sh --status` also shows the current URL.

Official release packages pull prebuilt images. A source checkout uses `MDP_VERSION=local` and builds images automatically, so the first launch needs to download dependencies. **Completing a local build does not mean the images have been published to GHCR.**

## Startup options

Run these commands from the project directory on your server or local machine. Use `./start.sh` on Linux / WSL and `.\start.ps1` in PowerShell. If Windows execution policy blocks the script, replace `.\start.ps1` in the examples with `powershell -ExecutionPolicy Bypass -File .\start.ps1` and keep the remaining arguments unchanged.

| Linux / WSL option | PowerShell option | Behavior |
|---|---|---|
| No arguments | No arguments | Start the web service and worker. Detect NVIDIA on first launch and choose CPU if no usable NVIDIA device is detected; subsequent launches reuse the last successful mode |
| `--cpu` | `-Cpu` | Explicitly use CPU and remember this mode after a successful start |
| `--gpu` | `-Gpu` | Explicitly use NVIDIA GPU and remember this mode after a successful start; fail if GPU environment checks fail |
| `--port 8080` | `-Port 8080` | Set the host port from 0–65535; 0 lets Docker allocate an available port |
| `--strict-port` | `-StrictPort` | Fail if the port is occupied instead of switching automatically; combine with a nonzero port when a fixed port is required |
| `--check` | `-Check` | Check the environment without starting the web service or a persistent worker; may build or pull images and run temporary diagnostic containers |
| `--status` | `-Status` | Show container status, the URL of running services, and the setup token if initialization is incomplete |
| `--stop` | `-Stop` | Stop the web service and worker while retaining containers, accounts, projects, models, and data volumes |
| `--build` | `-Build` | Force an image build from source before starting or checking; requires a full source checkout and does not clear the build cache |
| `--help` | No equivalent option; use `Get-Help .\start.ps1` | Show script usage; PowerShell uses its built-in help command |

Choose only one of `--cpu` / `--gpu`, and only one operation from `--check` / `--status` / `--stop`. With no operation specified, the script starts the services. Port, device mode, and strict-port settings can be combined. Bash also accepts `--port=8080`.

Common combinations:

| Scenario | Linux / WSL | PowerShell |
|---|---|---|
| Start with GPU on port 8080, switching ports if occupied | `./start.sh --gpu --port 8080` | `.\start.ps1 -Gpu -Port 8080` |
| Start with CPU on a fixed port 8080 | `./start.sh --cpu --port 8080 --strict-port` | `.\start.ps1 -Cpu -Port 8080 -StrictPort` |
| Allocate a port automatically | `./start.sh --port 0` | `.\start.ps1 -Port 0` |
| Check GPU availability | `./start.sh --gpu --check` | `.\start.ps1 -Gpu -Check` |
| Rebuild and start after source changes | `./start.sh --build` | `.\start.ps1 -Build` |

Port precedence is: command-line argument > `MDP_PORT` in the current shell environment > `MDP_PORT` in `.env` > default `8000`. After a successful start, the actual port is written back to `.env`. If the shell still sets `MDP_PORT`, that value takes precedence on the next launch. Automatic port switching only handles occupied ports; use the command output to diagnose other errors.

## Stopping and restarting

Services run in the background after startup, so closing the terminal does not stop them. To stop:

```bash
./start.sh --stop
```

Windows PowerShell equivalent:

```powershell
.\start.ps1 -Stop
```

Stopping interrupts active training or inference, so wait for tasks to finish when possible. Data is retained. Run `./start.sh` / `.\start.ps1` again to start the services. To restart, stop them first and run the start command after the stop succeeds. Starting again performs the normal image and environment checks; it does not automatically resume interrupted training.

Check running services and their URL with `./start.sh --status` / `.\start.ps1 -Status`. Stop and status commands read the project configuration, so you do not need to specify the port again.

## Remote access and common settings

On first run, the launcher creates `.env` from `.env.example`. Edit the relevant settings in `.env`, keeping your other configuration, and run the start command again to apply changes.

| Setting | Default | Purpose |
|---|---|---|
| `MDP_BIND_ADDRESS` | `127.0.0.1` | Local access only by default; use `0.0.0.0` for LAN / VPN access |
| `MDP_PORT` | `8000` | Host port; the actual port is saved after a successful start |
| `MDP_GPU_DEVICES` | `0` | Physical GPUs visible in GPU mode; accepts comma-separated indices or UUIDs |

To access the service from another computer on your LAN or VPN, set the following in the server's `.env`:

```dotenv
MDP_BIND_ADDRESS=0.0.0.0
MDP_PORT=8080
```

Then run `./start.sh --strict-port` (PowerShell: `.\start.ps1 -StrictPort`) and open `http://SERVER_IP:8080` on your computer, for example `http://192.168.1.100:8080`. Remote clients must use the server's actual IP address even if the launcher prints `127.0.0.1`. The server firewall or cloud security group must allow access to the selected port. If that port is occupied, change it or remove strict mode and use the newly allocated port, updating the network rules accordingly.

For personal access to a public server, use SSH forwarding while keeping the service bound to `127.0.0.1`. For multiple users across networks, use an HTTPS reverse proxy. See [Remote server access](docs/remote-server.md) for startup, forwarding, and domain configuration. Training uses the deployment server's GPU; the computer running the browser does not need one.

## First use

1. Sign in, create a project, and select its task type. For AI pre-annotation, prepare the appropriate model under **Model services**. Manual annotation and creating a training job do not require downloading training weights first.
2. Define classes and upload images. Use AI pre-annotation followed by manual corrections, or annotate manually from the start. For team collaboration, an administrator creates work blocks and members claim their own blocks before annotating. Block owners can also run AI on images in their block that have not yet been submitted for review, preserving manually drawn and manually corrected annotations.
3. Review the data: press `Y` to approve an image or `X` to open the rejection dialog; the rejection reason is optional. After a successful action, the next pending image opens automatically. Block reviews stay within the block and return to the work-block list when finished; project reviews return to the pending-review list on the data page. Then create a dataset version and split it into training and validation sets.
4. Create a training job, filter by name or model family, and select a starting model and compute device. There are 85 built-in options: 38 for detection, 17 for instance segmentation, and 15 each for classification and keypoints. See [Model selection and initialization](docs/user-guide.md#选择模型与初始化方式). **Use pretrained weights** is enabled by default; missing official weights download automatically during task preparation. Disable it to initialize randomly from the model architecture. Administrators can **Upload custom .pt weights** in the training dialog. For your first run, use a small image set and 1–3 epochs to verify the workflow. If the start button is disabled, check the reason displayed beside it; see [Training troubleshooting](docs/installation.md#开始新训练按钮不可点击).
5. After training, download the model or choose **Download inference deployment package** and start the prediction API on your target machine.

Official training models download on demand from fixed sources, with file-size and SHA-256 verification and support for resuming interrupted downloads. Training records show download progress. Preparation can be canceled, and failed downloads can be retried; the GPU is not occupied until downloads finish. For offline use, download models in advance under **Model services**, or have an administrator import compatible Ultralytics `.pt` weights. Existing models with the same name are not overwritten. The model list can be filtered by detection, segmentation, classification, and keypoint tasks.

AI annotation supports local Qwen3.5 inference. Downloads start in the background after you explicitly select a model, or you can download it in advance under **Model services**. The 0.8B weights are approximately 1.77 GB, and 35B-A3B approximately 71.93 GB; choose according to your available RAM and VRAM. Download SAM2 / SAM3 weights yourself, place them in the directory shown in the interface, and select **Recheck** to detect them. The project does not automatically download SAM weights. SAM3 supports both object detection and instance segmentation, producing bounding boxes or polygons according to the project type. See [Built-in local annotation models](docs/user-guide.md#内置本地标注模型) for default paths, filenames, and Docker copy commands.

Administrators can select **Delete** on a project card and enter the full project name to permanently delete the project, its dataset versions, and training artifacts. Deletion is blocked while tasks are queued or running, or images are being edited. Use **Archive** to retain a project. See [Deleting projects](docs/user-guide.md#删除项目).

A single GPU is used serially by default: annotation waits for active training, and previews show a notice when the GPU is busy. Cached models are released after GPU annotation to keep idle models from occupying training VRAM. For multiple GPUs, set visible devices through `MDP_GPU_DEVICES` in `.env`.

## Data and routine administration

Data and models are stored in the persistent Compose volumes `visionforge-data` and `visionforge-models`, respectively. Normal restarts and image upgrades preserve them. **`docker compose down -v` deletes these volumes.**

Back up both volumes before upgrading. Existing installations moving to a new release should first read [Migration and backups](docs/operations.md), especially the notes on machine-specific model paths used by older versions.

## API unavailable after an upgrade

Running `git pull` only updates source files; an already running Python process may still serve the old API. When the updated interface requests work-block endpoints, an older backend returns 404. This version mismatch does not mean project data was deleted. The updated interface explicitly asks you to update the service. For confirmed legacy projects without work blocks enabled, the original annotation and review workflow remains available.

For a Docker deployment from source, wait for active tasks to finish, stop the services, back up as described above, then update, rebuild, and start the web service and worker:

```bash
./start.sh --stop
# Continue after completing the backup
git pull
./start.sh --build
```

The launcher reuses the saved CPU / GPU mode and `.env` configuration. If you previously needed command-line port or strict-port settings, keep passing them, for example `./start.sh --gpu --port 8080 --strict-port --build`. The PowerShell equivalents are `.\start.ps1 -Stop`, `git pull`, and `.\start.ps1 -Build`, preserving the same `-Cpu` / `-Gpu`, `-Port`, and `-StrictPort` settings. Refresh the browser after startup succeeds; use `Ctrl+Shift+R` for a hard refresh if needed.

If running directly with Python, press `Ctrl+C` in the original terminal to stop the service, back up, run `git pull`, install the updated dependencies for your current CPU / GPU mode, and restart with the original environment variables, address, and port. For example, local development uses `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`. Restart any separately launched worker with its original command as well. If a service manager controls the processes, restart the corresponding web and worker services. Finally, refresh the browser so the frontend and backend come from the same update.

## Documentation

The detailed guides below are currently in Chinese. A [Chinese README](README.zh-CN.md) is also available.

- [Modules and features](docs/modules-and-features.md): functionality, role permissions, data flow, and source code index
- [Production deployment and launch review](docs/production.md)
- [Installation, GPU checks, and troubleshooting](docs/installation.md)
- [Remote server access](docs/remote-server.md)
- [Model inference deployment](docs/deployment.md)
- [reCamera Pro capture and model deployment](docs/recamera-pro.md): Connector installation, automatic imports, system model publishing and activation, capability checks, and legacy deployment compatibility
- [Detailed user guide](docs/user-guide.md)
- [Architecture and development](docs/architecture.md)
- [Contributing](CONTRIBUTING.md)
- [Migration, backups, and upgrades](docs/operations.md)
- [Packaging releases](docs/releases.md)
- [Third-party component and model licenses](THIRD_PARTY_NOTICES.md)

## Development and testing

Run these commands from a source checkout. Release installers include only startup configuration and documentation.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements/worker-cpu.txt
# Use requirements/worker-gpu.txt for NVIDIA development environments
cp .env.example .env
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# In another terminal, retrieve the initial setup token
.venv/bin/python -m app.setup
```

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pipeline.py
.venv/bin/python scripts/smoke_training_initialization.py
.venv/bin/python scripts/smoke_open_source.py --device cpu
# Single-GPU validation; set CUDA_VISIBLE_DEVICES first if needed
.venv/bin/python scripts/smoke_open_source.py --device cuda:0
```

Smoke tests run the pipeline in temporary directories with synthetic images and randomly initialized weights. They verify execution, not model accuracy. Add `--download-official` to the training-initialization smoke test to verify actual official weight downloads and training; it does not download weights by default. See [Verification records](docs/verification.md) for detailed coverage.
