from alembic import command
from alembic.config import Config
from skyvern.constants import REPO_ROOT_DIR


def migrate_db() -> None:
    alembic_cfg = Config()
    path = f"{REPO_ROOT_DIR}/alembic"
    alembic_cfg.set_main_option("script_location", path)
    command.upgrade(alembic_cfg, "head")
