# coding: utf-8
"""凭据 cell（Identity Cell）——vendor 无关的隔离单元.

一个 cell = 一个独立根目录 + 独立 leader socket + 仅含引用的元数据：

- **目录即隔离边界**：vendor 的 HOME 类环境变量应全部重定向到 cell 根目录
  （:meth:`CredentialCell.env_overrides` 给出通用两项 ``HOME``/``USERPROFILE``；
  vendor 专属 home 变量由各 vendor adapter 自行追加）。leader socket 按 cell
  命名（``leader-<cell_id>.sock``）——默认名是机器级共享资源，多 cell 并存时必须隔离。
- **元数据只存引用**：``broker-cell.json`` 只写 cell_id / leader_socket / login_state_ref
  等 URI 引用与时间戳，任何凭据值、token、密钥材料零落盘。
- **vendor 配置由回调生成**：broker 框架不认识任何 vendor 的配置格式；
  ``config_writer(cell) -> str`` 由 vendor adapter 提供，框架只负责落盘与收紧 ACL。
- **最小权限**：0600 等价（POSIX chmod；Windows 尽力而为 icacls 关继承仅留本人）。
"""
from __future__ import annotations

import json
import os
import subprocess

from .clock import SystemClock

__all__ = ["CredentialCell", "restrict_perms"]

_REF_RE_DELEGATED = None  # 引用格式校验交给上层（registry 已有同款红线）


def restrict_perms(path):
    """0600 等价（Windows 尽力而为）：chmod + icacls 关继承仅留本人。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        try:
            subprocess.run(["icacls", path, "/inheritance:r",
                            "/grant:r", "%s:F" % os.environ.get("USERNAME", "Administrator")],
                           capture_output=True, check=False)
        except Exception:
            pass


class CredentialCell(object):
    """vendor 无关凭据 cell。"""

    CONFIG_FILENAME = "config.toml"       # vendor 配置文件名（惯例；可覆盖）
    METADATA_FILENAME = "broker-cell.json"

    def __init__(self, root_dir, cell_id, vendor="", login_state_ref="",
                 config_writer=None, config_filename=None, clock=None):
        """
        ``config_writer``: callable(cell) -> str，生成 vendor 配置全文；
        为 None 时仅建骨架（不写 vendor 配置，供纯框架测试）。
        """
        if not cell_id or not str(cell_id).strip():
            raise ValueError("cell_id must be a non-empty string")
        self.root = os.path.abspath(root_dir)
        self.cell_id = str(cell_id)
        self.vendor = str(vendor or "")
        self.login_state_ref = str(login_state_ref or "")
        self.config_writer = config_writer
        self.config_filename = config_filename or self.CONFIG_FILENAME
        self.clock = clock or SystemClock()
        # leader socket 按 cell 隔离：默认名是机器级共享，多 cell 必须各自独立
        self.leader_socket = os.path.join(self.root, "leader-%s.sock" % self.cell_id)

    # ------------------------------------------------------------- 路径

    @property
    def config_path(self):
        return os.path.join(self.root, self.config_filename)

    @property
    def metadata_path(self):
        return os.path.join(self.root, self.METADATA_FILENAME)

    @property
    def sessions_dir(self):
        return os.path.join(self.root, "sessions")

    # ------------------------------------------------------------- 创建

    def create(self):
        """落盘 cell 骨架 + vendor 配置（回调生成）+ 仅引用的元数据。返回元数据 dict。"""
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.sessions_dir, exist_ok=True)
        if self.config_writer is not None:
            text = self.config_writer(self)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("config_writer must return non-empty config text")
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            restrict_perms(self.config_path)
        meta = self.metadata()
        with open(self.metadata_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        restrict_perms(self.metadata_path)
        return meta

    def metadata(self):
        """仅引用与时间戳的元数据（红线：任何凭据值不进元数据）。"""
        return {
            "cell_id": self.cell_id,
            "vendor": self.vendor,
            "leader_socket": self.leader_socket,
            "login_state_ref": self.login_state_ref,   # URI 引用，非值
            "created_at": self.clock.now(),
            "tokens": "reference-only",
        }

    # ------------------------------------------------------------- 隔离环境

    def env_overrides(self):
        """通用 HOME 类隔离（vendor 专属 home 变量请由 vendor adapter 追加）。"""
        return {
            "HOME": self.root,
            "USERPROFILE": self.root,
        }

    def exists(self):
        return os.path.isdir(self.root)
