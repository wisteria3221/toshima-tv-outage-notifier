"""設定モジュールのテスト"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


class TestDotenvLoading:
    """.env ファイルの読み込みタイミングのテスト"""

    def test_dry_run_from_dotenv_is_honored(self, tmp_path):
        """.env の DRY_RUN=true が import 時点の設定値に反映されること

        以前は main.py で load_dotenv() を呼んでいたため、config.py の import 時に
        評価される DRY_RUN / LOG_LEVEL に .env の値が間に合わず、DRY RUN のつもりで
        本番投稿される状態だった。python-dotenv は呼び出し元ファイルの位置から上へ
        .env を探すので、src/ を一時ディレクトリへ複製してその直上に .env を置く。
        """
        shutil.copytree(PROJECT_ROOT / "src", tmp_path / "src")
        (tmp_path / ".env").write_text(
            "DRY_RUN=true\nLOG_LEVEL=DEBUG\n", encoding="utf-8"
        )

        env = {k: v for k, v in os.environ.items() if k not in ("DRY_RUN", "LOG_LEVEL")}
        env["PYTHONPATH"] = str(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import src.config, src.notifier;"
                "print(src.config.DRY_RUN, src.notifier.DRY_RUN, src.config.LOG_LEVEL)",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "True True DEBUG"
