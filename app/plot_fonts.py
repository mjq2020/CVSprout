"""Keep training plots independent of Ultralytics' implicit font downloads."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile


def prepare_training_fonts() -> None:
    import matplotlib
    from ultralytics.utils import USER_CONFIG_DIR

    directory = Path(USER_CONFIG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    fallback = Path(matplotlib.get_data_path()) / "fonts/ttf/DejaVuSans.ttf"
    # Prefer a locally installed CJK font when available. No network requests.
    unicode_font = Path(os.getenv("MDP_PLOT_FONT", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"))
    if not unicode_font.is_file():
        unicode_font = fallback
    for filename, source in (("Arial.ttf", fallback), ("Arial.Unicode.ttf", unicode_font)):
        target = directory / filename
        if target.exists():
            continue
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".font", delete=False) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as original:
                shutil.copyfileobj(original, stream)
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
