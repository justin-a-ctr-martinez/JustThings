#!/usr/bin/env python3
"""
SVNMVP - SVN Management Tool
A comprehensive GUI and CLI tool for managing SVN repositories on macOS.

Usage:
    python svnmvp.py --gui                    # Launch GUI (default)
    python svnmvp.py checkout --url ... --dest ...
    python svnmvp.py update --path ...
    python svnmvp.py commit --path ... --message ...
    python svnmvp.py branch-create --repo-root ... --name ... --message ...
"""

import argparse
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import xml.etree.ElementTree as ET
import queue
import webbrowser


# =============================================================================
# 1. CONSTANTS AND DATACLASSES
# =============================================================================

VERSION = "1.0.0"
APP_NAME = "SVNMVP"

@dataclass
class AppPaths:
    """Application directory and file paths."""
    config_dir: Path = field(default_factory=lambda: Path.home() / "Library" / "Application Support" / "SVNMVP")
    config_file: Path = field(init=False)
    i18n_dir: Path = field(init=False)
    log_dir: Path = field(init=False)
    log_file: Path = field(init=False)

    def __post_init__(self):
        # Compute dependent paths
        self.config_file = self.config_dir / "config.json"
        self.i18n_dir = self.config_dir / "i18n"
        self.log_dir = Path.cwd() / "logs"
        self.log_file = self.log_dir / "svnmvp.log"

        # Create directories if they don't exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.i18n_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


class Theme(Enum):
    """UI theme options."""
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


@dataclass
class ThemeColors:
    """Color scheme definitions."""
    bg: str
    fg: str
    select_bg: str
    select_fg: str
    entry_bg: str
    entry_fg: str
    button_bg: str
    button_fg: str
    frame_bg: str
    text_bg: str
    text_fg: str
    accent: str
    error: str
    success: str
    warning: str


# Theme definitions
THEMES = {
    Theme.LIGHT: ThemeColors(
        bg="#ffffff", fg="#000000", select_bg="#0078d4", select_fg="#ffffff",
        entry_bg="#ffffff", entry_fg="#000000", button_bg="#f0f0f0",
        button_fg="#000000", frame_bg="#f8f8f8", text_bg="#ffffff",
        text_fg="#000000", accent="#0078d4", error="#d83b01",
        success="#107c10", warning="#ff8c00"
    ),
    Theme.DARK: ThemeColors(
        bg="#1e1e1e", fg="#ffffff", select_bg="#0078d4", select_fg="#ffffff",
        entry_bg="#2d2d30", entry_fg="#ffffff", button_bg="#3c3c3c",
        button_fg="#ffffff", frame_bg="#252526", text_bg="#1e1e1e",
        text_fg="#ffffff", accent="#0078d4", error="#f85149",
        success="#56d364", warning="#d29922"
    )
}


@dataclass
class SvnResult:
    """Result of SVN command execution."""
    stdout: str
    stderr: str
    exit_code: int
    elapsed: float
    command: str
    parsed_info: Optional[Dict[str, Any]] = None


@dataclass
class ActionParameter:
    """Parameter definition for SVN actions."""
    name: str
    type: str  # 'string', 'path', 'url', 'int', 'bool', 'choice'
    label: str
    description: str
    required: bool = False
    default: Any = None
    choices: Optional[List[str]] = None
    file_types: Optional[List[Tuple[str, str]]] = None  # For file dialogs


@dataclass
class ActionDefinition:
    """Definition of an SVN action."""
    id: str
    label: str
    description: str
    category: str
    parameters: List[ActionParameter] = field(default_factory=list)
    advanced: bool = False
    composite: bool = False  # True for workflow actions


@dataclass
class WorkingCopy:
    """Information about an SVN working copy."""
    path: str
    url: str
    revision: str
    repository_root: str
    uuid: str
    last_changed_rev: str
    last_changed_date: str
    locked: bool = False
    status: str = "Unknown"


@dataclass
class Config:
    """Application configuration."""
    svn_binary_path: str = "svn"
    theme: str = Theme.SYSTEM.value
    pagination_size: int = 100
    diff_line_limit: int = 2000
    blame_line_limit: int = 2000
    use_keychain: bool = True
    auto_detect_wc: bool = True
    working_copies: List[Dict[str, str]] = field(default_factory=list)
    recent_repos: List[str] = field(default_factory=list)
    window_geometry: str = "1200x800"
    default_trunk_path: str = "trunk"
    default_branches_path: str = "branches"
    default_tags_path: str = "tags"
    finder_quick_actions: bool = False


# Default I18N strings (English)
DEFAULT_I18N = {
    "app_title": "SVN MVP - SVN Management Tool",
    "working_copies": "Working Copies",
    "actions": "Actions",
    "activity": "Activity",
    "repo_browser": "Repository Browser",
    "settings": "Settings",
    "run": "Run",
    "cancel": "Cancel",
    "add_wc": "Add Working Copy",
    "remove_wc": "Remove Working Copy",
    "refresh": "Refresh",
    "show_advanced": "Show Advanced Actions",
    "svn_binary_path": "SVN Binary Path",
    "theme": "Theme",
    "pagination_size": "Pagination Size",
    "use_keychain": "Use macOS Keychain",
    "error": "Error",
    "success": "Success",
    "warning": "Warning",
    "info": "Information",
    "choose_directory": "Choose Directory",
    "choose_file": "Choose File",
    "no_wc_selected": "No working copy selected",
    "wc_busy": "Working copy is busy",
    "action_in_progress": "Action in progress",
    "action_completed": "Action completed",
    "action_failed": "Action failed",
    "confirm_action": "Confirm Action"
}


# =============================================================================
# 2. CONFIG AND STATE MANAGEMENT
# =============================================================================

class ConfigManager:
    """Manages application configuration."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._config = Config()
        self.load()

    def load(self) -> None:
        """Load configuration from file."""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
                    # Update config with loaded data
                    for key, value in data.items():
                        if hasattr(self._config, key):
                            setattr(self._config, key, value)
            except Exception as e:
                logging.warning(f"Failed to load config: {e}")

    def save(self) -> None:
        """Save configuration to file."""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(asdict(self._config), f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save config: {e}")

    @property
    def config(self) -> Config:
        return self._config


class I18NManager:
    """Manages internationalization strings."""

    def __init__(self, i18n_dir: Path):
        self.i18n_dir = i18n_dir
        self.strings = DEFAULT_I18N.copy()
        self.load("en")

    def load(self, locale: str) -> None:
        """Load strings for given locale."""
        locale_file = self.i18n_dir / f"{locale}.json"
        if locale_file.exists():
            try:
                with open(locale_file, 'r') as f:
                    data = json.load(f)
                    self.strings.update(data)
            except Exception as e:
                logging.warning(f"Failed to load locale {locale}: {e}")

    def get(self, key: str, default: Optional[str] = None) -> str:
        """Get localized string."""
        return self.strings.get(key, default or key)

    def __getitem__(self, key: str) -> str:
        return self.get(key)


# =============================================================================
# 3. KEYCHAIN AND CREDENTIAL LAYER
# =============================================================================

class CredentialStore:
    """Manages credentials using macOS Keychain via security CLI."""

    def __init__(self, use_keychain: bool = True):
        self.use_keychain = use_keychain and platform.system() == "Darwin"

    def get_credential(self, service: str, account: str) -> Optional[str]:
        """Retrieve credential from keychain."""
        if not self.use_keychain:
            return None

        try:
            cmd = [
                "/usr/bin/security", "find-internet-password",
                "-s", service, "-a", account, "-w"
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logging.debug(f"Failed to get credential: {e}")

        return None

    def set_credential(self, service: str, account: str, password: str) -> bool:
        """Store credential in keychain."""
        if not self.use_keychain:
            return False

        try:
            # Delete existing entry first
            self.delete_credential(service, account)

            cmd = [
                "/usr/bin/security", "add-internet-password",
                "-s", service, "-a", account, "-w", password
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            logging.debug(f"Failed to set credential: {e}")
            return False

    def delete_credential(self, service: str, account: str) -> bool:
        """Delete credential from keychain."""
        if not self.use_keychain:
            return False

        try:
            cmd = [
                "/usr/bin/security", "delete-internet-password",
                "-s", service, "-a", account
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            logging.debug(f"Failed to delete credential: {e}")
            return False


# =============================================================================
# 4. SVN COMMAND LAYER
# =============================================================================

class SvnRunner:
    """Wrapper for SVN command execution."""

    def __init__(self, svn_binary: str = "svn", timeout: int = 300):
        self.svn_binary = svn_binary
        self.timeout = timeout
        self.credential_store = CredentialStore()

        # Validate SVN binary
        if not self._validate_svn():
            raise ValueError(f"SVN binary not found or invalid: {svn_binary}")

    def _validate_svn(self) -> bool:
        """Validate SVN binary exists and works."""
        try:
            result = subprocess.run(
                [self.svn_binary, "--version"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except:
            return False

    def run(self, args: List[str], cwd: Optional[str] = None,
            input_data: Optional[str] = None) -> SvnResult:
        """Execute SVN command and return structured result."""
        full_cmd = [self.svn_binary] + args + ["--non-interactive"]
        cmd_str = " ".join(full_cmd)

        # Sanitize command string for logging (remove passwords)
        log_cmd = re.sub(r'(--password\s+)\S+', r'\1***', cmd_str)
        logging.info(f"Executing: {log_cmd}")

        start_time = time.time()
        try:
            # Clean environment
            env = os.environ.copy()
            env.pop('SVN_EDITOR', None)

            result = subprocess.run(
                full_cmd,
                cwd=cwd,
                input=input_data,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env
            )

            elapsed = time.time() - start_time

            # Parse svn info output if applicable
            parsed_info = None
            if "info" in args and result.returncode == 0:
                parsed_info = self._parse_info_output(result.stdout)

            return SvnResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                elapsed=elapsed,
                command=cmd_str,
                parsed_info=parsed_info
            )

        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            return SvnResult(
                stdout="",
                stderr=f"Command timed out after {self.timeout}s",
                exit_code=-1,
                elapsed=elapsed,
                command=cmd_str
            )
        except Exception as e:
            elapsed = time.time() - start_time
            return SvnResult(
                stdout="",
                stderr=str(e),
                exit_code=-1,
                elapsed=elapsed,
                command=cmd_str
            )

    def _parse_info_output(self, output: str) -> Dict[str, str]:
        """Parse svn info output into dictionary."""
        info = {}
        for line in output.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                info[key.strip()] = value.strip()
        return info

    def get_working_copy_info(self, path: str) -> Optional[WorkingCopy]:
        """Get working copy information."""
        result = self.run(["info", path])
        if result.exit_code == 0 and result.parsed_info:
            try:
                return WorkingCopy(
                    path=path,
                    url=result.parsed_info.get("URL", ""),
                    revision=result.parsed_info.get("Revision", ""),
                    repository_root=result.parsed_info.get("Repository Root", ""),
                    uuid=result.parsed_info.get("Repository UUID", ""),
                    last_changed_rev=result.parsed_info.get("Last Changed Rev", ""),
                    last_changed_date=result.parsed_info.get("Last Changed Date", ""),
                )
            except Exception as e:
                logging.error(f"Failed to parse working copy info: {e}")
        return None

    def is_working_copy(self, path: str) -> bool:
        """Check if path is an SVN working copy."""
        svn_dir = Path(path) / ".svn"
        return svn_dir.exists() and svn_dir.is_dir()

    def has_uncommitted_changes(self, path: str) -> bool:
        """Return True if working copy at path has uncommitted changes."""
        # Use `svn status` --if output is non-empty then changes exist
        try:
            result = self.run(["status", path])
            if result.exit_code == 0:
                # SVN status prints nothing when clean; any non-whitespace output indicates changes
                return bool(result.stdout.strip())
            # If status failed assume safe side: mark as changed
            return True
        except Exception:
            return True

    # Additional convenience methods (from newFunctions.py)
    def list_url(self, url: str, revision: Optional[str] = None,
                 verbose: bool = False, recursive: bool = False) -> SvnResult:
        """
        List repository URL contents (wraps `svn list`).
        Returns SvnResult with stdout containing the raw listing.
        """
        args = ["list", url]
        if revision:
            args.extend(["-r", str(revision)])
        if verbose:
            args.append("--verbose")
        if recursive:
            args.append("--recursive")
        return self.run(args)

    def get_log(self, path_or_url: str, revision: Optional[str] = None,
                limit: Optional[int] = None, verbose: bool = False,
                stop_on_copy: bool = False) -> SvnResult:
        """
        Retrieve svn log for a path or URL (wraps `svn log`).
        Returns SvnResult with stdout containing the raw log (XML if requested).
        """
        args = ["log", path_or_url, "--xml"]
        if revision:
            args.extend(["-r", str(revision)])
        if limit is not None:
            args.extend(["--limit", str(limit)])
        if verbose:
            args.append("--verbose")
        if stop_on_copy:
            args.append("--stop-on-copy")
        return self.run(args)


# =============================================================================
# 5. ACTION REGISTRY
# =============================================================================

class ActionRegistry:
    """Registry of all available SVN actions."""

    def __init__(self):
        self.actions: Dict[str, ActionDefinition] = {}
        self._register_actions()

    def _register_actions(self):
        """Register all SVN actions."""

        # Basic workspace management
        self.register(ActionDefinition(
            id="checkout",
            label="Checkout",
            description="Check out a working copy from a repository",
            category="workspace",
            parameters=[
                ActionParameter("url", "url", "Repository URL", "URL to check out from", True),
                ActionParameter("dest", "path", "Destination", "Local directory path", True),
                ActionParameter("revision", "string", "Revision", "Specific revision to checkout"),
                ActionParameter("depth", "choice", "Depth", "Checkout depth",
                                choices=["infinity", "immediates", "files", "empty"]),
            ]
        ))

        self.register(ActionDefinition(
            id="update",
            label="Update",
            description="Update working copy to latest revision",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Working Copy", "Path to working copy", True),
                ActionParameter("revision", "string", "Revision", "Update to specific revision"),
            ]
        ))

        self.register(ActionDefinition(
            id="status",
            label="Status",
            description="Show working copy status",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Working Copy", "Path to working copy", True),
                ActionParameter("verbose", "bool", "Verbose", "Show detailed status"),
                ActionParameter("show_updates", "bool", "Show Updates", "Show out-of-date information"),
            ]
        ))

        self.register(ActionDefinition(
            id="add",
            label="Add",
            description="Add files or directories to version control",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory to add", True),
                ActionParameter("force", "bool", "Force", "Force addition"),
                ActionParameter("parents", "bool", "Parents", "Add parent directories as needed"),
            ]
        ))

        self.register(ActionDefinition(
            id="delete",
            label="Delete",
            description="Remove files or directories from version control",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory to delete", True),
                ActionParameter("force", "bool", "Force", "Force deletion"),
                ActionParameter("keep_local", "bool", "Keep Local", "Don't delete local files"),
            ]
        ))

        self.register(ActionDefinition(
            id="move",
            label="Move/Rename",
            description="Move or rename files and directories",
            category="workspace",
            parameters=[
                ActionParameter("src", "path", "Source", "Source path", True),
                ActionParameter("dest", "path", "Destination", "Destination path", True),
                ActionParameter("force", "bool", "Force", "Force move"),
            ]
        ))

        self.register(ActionDefinition(
            id="copy",
            label="Copy",
            description="Copy files and directories",
            category="workspace",
            parameters=[
                ActionParameter("src", "path", "Source", "Source path", True),
                ActionParameter("dest", "path", "Destination", "Destination path", True),
                ActionParameter("revision", "string", "Revision", "Copy from specific revision"),
            ]
        ))

        self.register(ActionDefinition(
            id="revert",
            label="Revert",
            description="Revert changes in working copy",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory to revert", True),
                ActionParameter("recursive", "bool", "Recursive", "Revert recursively"),
            ]
        ))

        self.register(ActionDefinition(
            id="cleanup",
            label="Cleanup",
            description="Clean up working copy",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Working Copy", "Path to working copy", True),
                ActionParameter("vacuum_pristines", "bool", "Vacuum Pristines", "Remove unreferenced pristines"),
            ]
        ))

        self.register(ActionDefinition(
            id="resolve",
            label="Resolve",
            description="Resolve conflicted files",
            category="workspace",
            parameters=[
                ActionParameter("path", "path", "Path", "Conflicted file path", True),
                ActionParameter("accept", "choice", "Accept", "Resolution strategy",
                                choices=["working", "base", "mine-conflict", "theirs-conflict",
                                        "mine-full", "theirs-full"]),
            ]
        ))

        # Changes and commits
        self.register(ActionDefinition(
            id="diff",
            label="Diff",
            description="Show differences",
            category="changes",
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory path"),
                ActionParameter("old_rev", "string", "Old Revision", "Old revision"),
                ActionParameter("new_rev", "string", "New Revision", "New revision"),
                ActionParameter("summarize", "bool", "Summarize", "Show summary only"),
            ]
        ))

        self.register(ActionDefinition(
            id="commit",
            label="Commit",
            description="Commit changes to repository",
            category="changes",
            parameters=[
                ActionParameter("path", "path", "Path", "Path to commit", True),
                ActionParameter("message", "string", "Message", "Commit message", True),
                ActionParameter("keep_locks", "bool", "Keep Locks", "Don't release locks"),
            ]
        ))

        self.register(ActionDefinition(
            id="merge",
            label="Merge",
            description="Merge changes between branches",
            category="changes",
            advanced=True,
            parameters=[
                ActionParameter("source", "url", "Source", "Source URL or path", True),
                ActionParameter("path", "path", "Target", "Target working copy", True),
                ActionParameter("revision", "string", "Revision Range", "Revision range (e.g., 100:200)"),
                ActionParameter("dry_run", "bool", "Dry Run", "Show what would be merged"),
                ActionParameter("record_only", "bool", "Record Only", "Record merge without changes"),
            ]
        ))

        # History and inspection
        self.register(ActionDefinition(
            id="log",
            label="Log",
            description="Show commit history",
            category="history",
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory path"),
                ActionParameter("revision", "string", "Revision Range", "Revision range"),
                ActionParameter("limit", "int", "Limit", "Maximum number of log entries", default=100),
                ActionParameter("verbose", "bool", "Verbose", "Show changed paths"),
                ActionParameter("stop_on_copy", "bool", "Stop on Copy", "Don't cross copies"),
            ]
        ))

        self.register(ActionDefinition(
            id="blame",
            label="Blame/Annotate",
            description="Show line-by-line authorship",
            category="history",
            parameters=[
                ActionParameter("path", "path", "File", "File to annotate", True),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("force", "bool", "Force", "Show binary files"),
            ]
        ))

        self.register(ActionDefinition(
            id="info",
            label="Info",
            description="Show information about files and directories",
            category="history",
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory path", True),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("show_item", "choice", "Show Item", "Information to display",
                                choices=["kind", "url", "relative-url", "repos-root-url", "repos-uuid",
                                         "revision", "last-changed-revision", "last-changed-date",
                                         "last-changed-author"]),
            ]
        ))

        # Repository operations
        self.register(ActionDefinition(
            id="list",
            label="List",
            description="List directory contents",
            category="repository",
            parameters=[
                ActionParameter("url", "url", "URL", "Repository URL", True),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("verbose", "bool", "Verbose", "Show detailed information"),
                ActionParameter("recursive", "bool", "Recursive", "List recursively"),
            ]
        ))

        self.register(ActionDefinition(
            id="mkdir",
            label="Make Directory",
            description="Create directory in repository",
            category="repository",
            parameters=[
                ActionParameter("url", "url", "URL", "Directory URL to create", True),
                ActionParameter("message", "string", "Message", "Commit message", True),
                ActionParameter("parents", "bool", "Parents", "Create parent directories"),
            ]
        ))

        self.register(ActionDefinition(
            id="import",
            label="Import",
            description="Import files into repository",
            category="repository",
            parameters=[
                ActionParameter("path", "path", "Local Path", "Local directory to import", True),
                ActionParameter("url", "url", "Repository URL", "Destination URL", True),
                ActionParameter("message", "string", "Message", "Commit message", True),
                ActionParameter("no_ignore", "bool", "No Ignore", "Don't ignore files matching global ignore patterns"),
            ]
        ))

        self.register(ActionDefinition(
            id="export",
            label="Export",
            description="Export clean directory tree",
            category="repository",
            parameters=[
                ActionParameter("url", "url", "Source URL", "Repository URL to export", True),
                ActionParameter("dest", "path", "Destination", "Local destination path", True),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("force", "bool", "Force", "Overwrite existing files"),
            ]
        ))

        # Property operations
        self.register(ActionDefinition(
            id="proplist",
            label="List Properties",
            description="List properties on files and directories",
            category="properties",
            advanced=True,
            parameters=[
                ActionParameter("path", "path", "Path", "File or directory path", True),
                ActionParameter("verbose", "bool", "Verbose", "Show property values"),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("revprop", "bool", "Revision Properties", "Show revision properties"),
            ]
        ))

        self.register(ActionDefinition(
            id="propget",
            label="Get Property",
            description="Get property value",
            category="properties",
            advanced=True,
            parameters=[
                ActionParameter("propname", "string", "Property Name", "Name of property", True),
                ActionParameter("path", "path", "Path", "File or directory path", True),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("strict", "bool", "Strict", "Don't print newline"),
            ]
        ))

        self.register(ActionDefinition(
            id="propset",
            label="Set Property",
            description="Set property on files and directories",
            category="properties",
            advanced=True,
            parameters=[
                ActionParameter("propname", "string", "Property Name", "Name of property", True),
                ActionParameter("propval", "string", "Property Value", "Value of property", True),
                ActionParameter("path", "path", "Path", "File or directory path", True),
                ActionParameter("recursive", "bool", "Recursive", "Apply recursively"),
                ActionParameter("force", "bool", "Force", "Force operation"),
            ]
        ))

        # Locking
        self.register(ActionDefinition(
            id="lock",
            label="Lock",
            description="Lock files in repository",
            category="locking",
            advanced=True,
            parameters=[
                ActionParameter("path", "path", "Path", "File to lock", True),
                ActionParameter("message", "string", "Message", "Lock message"),
                ActionParameter("force", "bool", "Force", "Steal existing lock"),
            ]
        ))

        self.register(ActionDefinition(
            id="unlock",
            label="Unlock",
            description="Unlock files in repository",
            category="locking",
            advanced=True,
            parameters=[
                ActionParameter("path", "path", "Path", "File to unlock", True),
                ActionParameter("force", "bool", "Force", "Break lock"),
            ]
        ))

        # Advanced operations
        self.register(ActionDefinition(
            id="switch",
            label="Switch",
            description="Switch working copy to different URL",
            category="advanced",
            advanced=True,
            parameters=[
                ActionParameter("url", "url", "URL", "New URL", True),
                ActionParameter("path", "path", "Path", "Working copy path", True),
                ActionParameter("revision", "string", "Revision", "Specific revision"),
                ActionParameter("force", "bool", "Force", "Force switch"),
                ActionParameter("ignore_ancestry", "bool", "Ignore Ancestry", "Ignore ancestry"),
            ]
        ))

        self.register(ActionDefinition(
            id="relocate",
            label="Relocate",
            description="Relocate working copy to new repository URL",
            category="advanced",
            advanced=True,
            parameters=[
                ActionParameter("from_url", "url", "From URL", "Current repository URL", True),
                ActionParameter("to_url", "url", "To URL", "New repository URL", True),
                ActionParameter("path", "path", "Path", "Working copy path", True),
            ]
        ))

        # Composite workflow actions
        self.register(ActionDefinition(
            id="branch-create",
            label="Create Feature Branch",
            description="Create a new feature branch from trunk",
            category="workflows",
            composite=True,
            parameters=[
                ActionParameter("repo_root", "url", "Repository Root", "Repository root URL", True),
                ActionParameter("name", "string", "Branch Name", "Name of the feature branch", True),
                ActionParameter("message", "string", "Message", "Commit message", True),
                ActionParameter("from_path", "string", "Source Path", "Source path (default: trunk)", default="trunk"),
            ]
        ))

        self.register(ActionDefinition(
            id="branch-sync",
            label="Sync Branch with Trunk",
            description="Merge trunk changes into feature branch",
            category="workflows",
            composite=True,
            parameters=[
                ActionParameter("branch_path", "path", "Branch Working Copy", "Feature branch working copy", True),
                ActionParameter("trunk_url", "url", "Trunk URL", "Trunk URL (auto-detected if empty)"),
                ActionParameter("dry_run", "bool", "Dry Run", "Show what would be merged"),
            ]
        ))

        self.register(ActionDefinition(
            id="branch-merge-to-trunk",
            label="Merge Branch to Trunk",
            description="Merge feature branch back into trunk",
            category="workflows",
            composite=True,
            parameters=[
                ActionParameter("trunk_path", "path", "Trunk Working Copy", "Trunk working copy", True),
                ActionParameter("branch_url", "url", "Branch URL", "Feature branch URL", True),
                ActionParameter("message", "string", "Message", "Merge commit message", True),
                ActionParameter("dry_run", "bool", "Dry Run", "Show what would be merged"),
                ActionParameter("record_only", "bool", "Record Only", "Record merge without changes"),
            ]
        ))

        self.register(ActionDefinition(
            id="tag-create",
            label="Create Release Tag",
            description="Create a release tag from trunk",
            category="workflows",
            composite=True,
            parameters=[
                ActionParameter("repo_root", "url", "Repository Root", "Repository root URL", True),
                ActionParameter("version", "string", "Version", "Release version (e.g., 1.0.0)", True),
                ActionParameter("message", "string", "Message", "Tag message", True),
                ActionParameter("from_path", "string", "Source Path", "Source path (default: trunk)", default="trunk"),
            ]
        ))

    def register(self, action: ActionDefinition):
        """Register an action."""
        self.actions[action.id] = action

    def get(self, action_id: str) -> Optional[ActionDefinition]:
        """Get action by ID."""
        return self.actions.get(action_id)

    def get_by_category(self, category: str) -> List[ActionDefinition]:
        """Get actions by category."""
        return [a for a in self.actions.values() if a.category == category]

    def get_basic_actions(self) -> List[ActionDefinition]:
        """Get basic (non-advanced) actions."""
        return [a for a in self.actions.values() if not a.advanced]

    def get_all_actions(self) -> List[ActionDefinition]:
        """Get all actions."""
        return list(self.actions.values())


# =============================================================================
# 6. WORKFLOW IMPLEMENTATIONS
# =============================================================================

class WorkflowExecutor:
    """Executes composite workflow actions."""

    def __init__(self, svn_runner: SvnRunner, config: Config):
        self.svn = svn_runner
        self.config = config

    def execute_branch_create(self, params: Dict[str, Any]) -> SvnResult:
        """Create feature branch from trunk."""
        repo_root = params["repo_root"].rstrip("/")
        branch_name = params["name"]
        message = params["message"]
        from_path = params.get("from_path", self.config.default_trunk_path)

        source_url = f"{repo_root}/{from_path}"
        dest_url = f"{repo_root}/{self.config.default_branches_path}/{branch_name}"

        return self.svn.run([
            "copy", source_url, dest_url,
            "-m", message
        ])

    def execute_branch_sync(self, params: Dict[str, Any]) -> SvnResult:
        """Sync branch with trunk."""
        branch_path = params["branch_path"]
        trunk_url = params.get("trunk_url")
        dry_run = params.get("dry_run", False)

        # Auto-detect trunk URL if not provided
        if not trunk_url:
            wc_info = self.svn.get_working_copy_info(branch_path)
            if not wc_info:
                return SvnResult("", "Failed to get working copy info", 1, 0, "branch-sync")

            # Assume standard layout: replace /branches/... with /trunk
            repo_root = wc_info.repository_root
            trunk_url = f"{repo_root}/{self.config.default_trunk_path}"

        args = ["merge", trunk_url, branch_path]
        if dry_run:
            args.append("--dry-run")

        return self.svn.run(args)

    def execute_branch_merge_to_trunk(self, params: Dict[str, Any]) -> SvnResult:
        """Merge branch back to trunk."""
        trunk_path = params["trunk_path"]
        branch_url = params["branch_url"]
        message = params["message"]
        dry_run = params.get("dry_run", False)
        record_only = params.get("record_only", False)

        args = ["merge", branch_url, trunk_path, "-m", message]
        if dry_run:
            args.append("--dry-run")
        if record_only:
            args.append("--record-only")

        return self.svn.run(args)

    def execute_tag_create(self, params: Dict[str, Any]) -> SvnResult:
        """Create release tag."""
        repo_root = params["repo_root"].rstrip("/")
        version = params["version"]
        message = params["message"]
        from_path = params.get("from_path", self.config.default_trunk_path)

        source_url = f"{repo_root}/{from_path}"
        tag_name = f"release-{version}" if not version.startswith("release-") else version
        dest_url = f"{repo_root}/{self.config.default_tags_path}/{tag_name}"

        return self.svn.run([
            "copy", source_url, dest_url,
            "-m", message
        ])


# =============================================================================
# 7. CONCURRENCY & JOB CONTROL
# =============================================================================

class JobManager:
    """Manages concurrent job execution with per-WC mutexes."""

    def __init__(self):
        self.active_jobs: Dict[str, threading.Event] = {}
        self.job_lock = threading.Lock()

    def can_start_job(self, working_copy: str) -> bool:
        """Check if a job can be started for the working copy."""
        with self.job_lock:
            return working_copy not in self.active_jobs

    def start_job(self, working_copy: str) -> bool:
        """Start a job for the working copy."""
        with self.job_lock:
            if working_copy in self.active_jobs:
                return False
            self.active_jobs[working_copy] = threading.Event()
            return True

    def finish_job(self, working_copy: str):
        """Finish a job for the working copy."""
        with self.job_lock:
            if working_copy in self.active_jobs:
                self.active_jobs[working_copy].set()
                del self.active_jobs[working_copy]

    def is_job_active(self, working_copy: str) -> bool:
        """Check if a job is active for the working copy."""
        with self.job_lock:
            return working_copy in self.active_jobs

    def cancel_job(self, working_copy: str):
        """Cancel a job for the working copy."""
        with self.job_lock:
            if working_copy in self.active_jobs:
                self.active_jobs[working_copy].set()


# =============================================================================
# 8. LOGGING SETUP
# =============================================================================

def setup_logging(log_file: Path, log_level: str = "INFO"):
    """Setup logging configuration."""
    log_level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }

    logging.basicConfig(
        level=log_level_map.get(log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )


# =============================================================================
# 9. CLI INTERFACE
# =============================================================================

class CLI:
    """Command-line interface."""

    def __init__(self):
        self.paths = AppPaths()
        self.config_manager = ConfigManager(self.paths.config_file)
        self.i18n = I18NManager(self.paths.i18n_dir)
        self.registry = ActionRegistry()
        self.svn = SvnRunner(self.config_manager.config.svn_binary_path)
        self.workflows = WorkflowExecutor(self.svn, self.config_manager.config)

    def create_parser(self) -> argparse.ArgumentParser:
        """Create argument parser."""
        parser = argparse.ArgumentParser(
            description="SVN Management Tool",
            formatter_class=argparse.RawDescriptionHelpFormatter
        )

        parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
        parser.add_argument("--gui", action="store_true", help="Launch GUI")
        parser.add_argument("--svn-binary", help="Path to SVN binary")
        parser.add_argument("--config", help="Config file path")
        parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                            default="INFO", help="Log level")
        parser.add_argument("--json", action="store_true", help="Output in JSON format")
        parser.add_argument("--timeout", type=int, default=300, help="Command timeout")
        parser.add_argument("--no-keychain", action="store_true", help="Disable keychain")

        subparsers = parser.add_subparsers(dest="command", help="Available commands")

        # Add subcommands for each action
        for action in self.registry.get_all_actions():
            self._add_action_subparser(subparsers, action)

        return parser

    def _add_action_subparser(self, subparsers: argparse._SubParsersAction,
                              action: ActionDefinition):
        """Add subparser for an action."""
        subparser = subparsers.add_parser(
            action.id,
            help=action.description,
            aliases=[action.id.replace("-", "_")]
        )

        for param in action.parameters:
            args = [f"--{param.name}"]
            kwargs = {"help": param.description}

            if param.required:
                kwargs["required"] = True
            if param.default is not None:
                kwargs["default"] = param.default

            if param.type == "bool":
                kwargs["action"] = "store_true"
            elif param.type == "int":
                kwargs["type"] = int
            elif param.type == "choice":
                kwargs["choices"] = param.choices

            subparser.add_argument(*args, **kwargs)

    def run(self, args: Optional[List[str]] = None) -> int:
        """Run CLI with given arguments."""
        parser = self.create_parser()
        parsed_args = parser.parse_args(args)

        # Setup logging
        setup_logging(self.paths.log_file, parsed_args.log_level)

        # Launch GUI if no command specified or --gui flag
        if not parsed_args.command or parsed_args.gui:
            return self._launch_gui()

        # Override config if needed
        if parsed_args.svn_binary:
            self.config_manager.config.svn_binary_path = parsed_args.svn_binary
        if parsed_args.no_keychain:
            self.config_manager.config.use_keychain = False

        # Execute command
        return self._execute_command(parsed_args)

    def _launch_gui(self) -> int:
        """Launch GUI."""
        try:
            gui = GUI(
                self.paths, self.config_manager, self.i18n,
                self.registry, self.svn, self.workflows
            )
            gui.run()
            return 0
        except Exception as e:
            logging.error(f"Failed to launch GUI: {e}")
            return 1

    def _execute_command(self, args: argparse.Namespace) -> int:
        """Execute CLI command."""
        action = self.registry.get(args.command)
        if not action:
            logging.error(f"Unknown command: {args.command}")
            return 1

        # Build parameters
        params = {}
        for param in action.parameters:
            value = getattr(args, param.name, param.default)
            if value is not None:
                params[param.name] = value

        # Execute action
        try:
            if action.composite:
                result = self._execute_workflow(action.id, params)
            else:
                result = self._execute_svn_action(action.id, params)

            # Output result
            if args.json:
                output = {
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "elapsed": result.elapsed
                }
                print(json.dumps(output, indent=2))
            else:
                if result.stdout:
                    print(result.stdout)
                if result.stderr:
                    print(result.stderr, file=sys.stderr)

            return result.exit_code

        except Exception as e:
            logging.error(f"Failed to execute command: {e}")
            return 1

    def _execute_workflow(self, workflow_id: str, params: Dict[str, Any]) -> SvnResult:
        """Execute workflow action."""
        if workflow_id == "branch-create":
            return self.workflows.execute_branch_create(params)
        elif workflow_id == "branch-sync":
            return self.workflows.execute_branch_sync(params)
        elif workflow_id == "branch-merge-to-trunk":
            return self.workflows.execute_branch_merge_to_trunk(params)
        elif workflow_id == "tag-create":
            return self.workflows.execute_tag_create(params)
        else:
            raise ValueError(f"Unknown workflow: {workflow_id}")

    def _execute_svn_action(self, action_id: str, params: Dict[str, Any]) -> SvnResult:
        """Execute SVN action."""
        args = [action_id]

        # Convert parameters to SVN arguments
        for key, value in params.items():
            if value is None:
                continue

            if isinstance(value, bool):
                if value:
                    args.append(f"--{key}")
            else:
                args.extend([f"--{key}", str(value)])

        return self.svn.run(args)


# =============================================================================
# 10. GUI LAYER
# =============================================================================

class GUI:
    """Tkinter-based graphical user interface."""

    def __init__(self, paths: AppPaths, config_manager: ConfigManager,
                 i18n: I18NManager, registry: ActionRegistry,
                 svn: SvnRunner, workflows: WorkflowExecutor):
        self.paths = paths
        self.config_manager = config_manager
        self.i18n = i18n
        self.registry = registry
        self.svn = svn
        self.workflows = workflows
        self.job_manager = JobManager()

        # GUI state
        self.root = None
        self.current_theme = Theme.SYSTEM
        # Tkinter variable objects are created after root is initialized
        self.show_advanced = None
        self.selected_wc = None
        self.working_copies: Dict[str, WorkingCopy] = {}

        # GUI components
        self.wc_listbox = None
        self.action_combobox = None
        self.param_frame = None
        self.param_widgets: Dict[str, tk.Widget] = {}
        self.log_text = None
        self.progress_var = None
        self.status_var = None

        # Message queue for thread communication
        self.message_queue = queue.Queue()

    def run(self):
        """Start the GUI (enhanced with login dialog).

        This version was moved from newFunctions.py and adds a login dialog
        before building the main UI. Behavior is unchanged for users who
        successfully connect; if the user cancels the login dialog the
        application exits gracefully.
        """
        self.root = tk.Tk()
        self.root.title(self.i18n["app_title"])
        self.root.geometry(self.config_manager.config.window_geometry)

        # Create tkinter variable objects now that root exists
        self.show_advanced = tk.BooleanVar(master=self.root, value=False)
        self.selected_wc = tk.StringVar(master=self.root)
        self.progress_var = tk.StringVar(master=self.root)
        self.status_var = tk.StringVar(master=self.root)

        # Before building the rest of the UI, present login dialog.
        # Keep prompting until successful login or user cancels.
        logged_in = self._show_login_dialog()
        if not logged_in:
            # User cancelled login; close application.
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        # Detect and set system theme
        self._detect_system_theme()
        self._setup_ui()
        self._apply_theme()
        self._discover_working_copies()

        # Start message queue processor
        self.root.after(100, self._process_message_queue)

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        self.root.mainloop()

    def _detect_system_theme(self):
        """Detect system theme preference."""
        try:
            # Try to detect macOS dark mode
            result = subprocess.run([
                "defaults", "read", "-g", "AppleInterfaceStyle"
            ], capture_output=True, text=True)

            if result.returncode == 0 and "dark" in result.stdout.lower():
                self.current_theme = Theme.DARK
            else:
                self.current_theme = Theme.LIGHT
        except:
            self.current_theme = Theme.LIGHT

        # Override with user preference
        config_theme = self.config_manager.config.theme
        if config_theme in [t.value for t in Theme]:
            if config_theme != Theme.SYSTEM.value:
                self.current_theme = Theme(config_theme)

    def _setup_ui(self):
        """Setup the user interface."""
        # Create main paned window
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Left panel - Working Copies
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=1)

        self._setup_left_panel(left_frame)

        # Right panel - Tabs
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=2)

        self._setup_right_panel(right_frame)

        # Bottom status bar
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Label(status_frame, textvariable=self.status_var).pack(side=tk.LEFT, padx=5)
        ttk.Label(status_frame, textvariable=self.progress_var).pack(side=tk.RIGHT, padx=5)

    def _setup_left_panel(self, parent: ttk.Frame):
        """Setup working copies panel."""
        # Title and controls
        title_frame = ttk.Frame(parent)
        title_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(title_frame, text=self.i18n["working_copies"],
                  font=("", 12, "bold")).pack(side=tk.LEFT)

        # Buttons
        btn_frame = ttk.Frame(title_frame)
        btn_frame.pack(side=tk.RIGHT)

        ttk.Button(btn_frame, text="+", width=3,
                   command=self._add_working_copy).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="-", width=3,
                   command=self._remove_working_copy).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="↻", width=3,
                   command=self._refresh_working_copies).pack(side=tk.LEFT, padx=2)

        # Working copies list
        list_frame = ttk.Frame(parent)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Listbox with scrollbar
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.wc_listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set)
        self.wc_listbox.pack(fill=tk.BOTH, expand=True)
        self.wc_listbox.bind('<<ListboxSelect>>', self._on_wc_select)

        scrollbar.config(command=self.wc_listbox.yview)

    def _setup_right_panel(self, parent: ttk.Frame):
        """Setup tabbed right panel."""
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        # Actions tab
        actions_frame = ttk.Frame(notebook)
        notebook.add(actions_frame, text=self.i18n["actions"])
        self._setup_actions_tab(actions_frame)

        # Activity tab
        activity_frame = ttk.Frame(notebook)
        notebook.add(activity_frame, text=self.i18n["activity"])
        self._setup_activity_tab(activity_frame)

        # Repository Browser tab
        repo_frame = ttk.Frame(notebook)
        notebook.add(repo_frame, text=self.i18n["repo_browser"])
        self._setup_repo_browser_tab(repo_frame)

        # Settings tab
        settings_frame = ttk.Frame(notebook)
        notebook.add(settings_frame, text=self.i18n["settings"])
        self._setup_settings_tab(settings_frame)

    def _setup_actions_tab(self, parent: ttk.Frame):
        """Setup actions tab."""
        # Action selection
        selection_frame = ttk.Frame(parent)
        selection_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(selection_frame, text="Action:").pack(side=tk.LEFT)

        self.action_combobox = ttk.Combobox(selection_frame, state="readonly")
        self.action_combobox.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        self.action_combobox.bind('<<ComboboxSelected>>', self._on_action_select)

        # Advanced toggle
        advanced_frame = ttk.Frame(parent)
        advanced_frame.pack(fill=tk.X, padx=10)

        ttk.Checkbutton(advanced_frame, text=self.i18n["show_advanced"],
                        variable=self.show_advanced,
                        command=self._update_action_list).pack(side=tk.LEFT)

        # Parameters frame
        self.param_frame = ttk.LabelFrame(parent, text="Parameters")
        self.param_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Control buttons
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Button(button_frame, text=self.i18n["run"],
                   command=self._execute_action).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text=self.i18n["cancel"],
                   command=self._cancel_action).pack(side=tk.LEFT, padx=5)

        self._update_action_list()

    def _setup_activity_tab(self, parent: ttk.Frame):
        """Setup activity/logs tab."""
        # Log display
        self.log_text = scrolledtext.ScrolledText(parent, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Log controls
        controls_frame = ttk.Frame(parent)
        controls_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(controls_frame, text="Clear",
                   command=self._clear_log).pack(side=tk.LEFT, padx=5)
        ttk.Button(controls_frame, text="Save Log",
                   command=self._save_log).pack(side=tk.LEFT, padx=5)

    def _setup_repo_browser_tab(self, parent: ttk.Frame):
        """Setup repository browser tab."""
        info_frame = ttk.LabelFrame(parent, text="Repository Information")
        info_frame.pack(fill=tk.X, padx=10, pady=10)

        self.repo_info_text = tk.Text(info_frame, height=6, state=tk.DISABLED)
        self.repo_info_text.pack(fill=tk.X, padx=5, pady=5)

        # Quick actions
        quick_frame = ttk.LabelFrame(parent, text="Quick Actions")
        quick_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Button(quick_frame, text="Browse Trunk",
                   command=lambda: self._browse_repo_path("trunk")).pack(side=tk.LEFT, padx=5, pady=5)
        ttk.Button(quick_frame, text="Browse Branches",
                   command=lambda: self._browse_repo_path("branches")).pack(side=tk.LEFT, padx=5, pady=5)
        ttk.Button(quick_frame, text="Browse Tags",
                   command=lambda: self._browse_repo_path("tags")).pack(side=tk.LEFT, padx=5, pady=5)

    def _setup_settings_tab(self, parent: ttk.Frame):
        """Setup settings tab."""
        # SVN binary path
        binary_frame = ttk.LabelFrame(parent, text=self.i18n["svn_binary_path"])
        binary_frame.pack(fill=tk.X, padx=10, pady=10)

        self.binary_var = tk.StringVar(value=self.config_manager.config.svn_binary_path)
        binary_entry = ttk.Entry(binary_frame, textvariable=self.binary_var)
        binary_entry.pack(fill=tk.X, padx=5, pady=5)

        ttk.Button(binary_frame, text="Browse...",
                   command=self._browse_svn_binary).pack(pady=5)

        # Theme settings
        theme_frame = ttk.LabelFrame(parent, text=self.i18n["theme"])
        theme_frame.pack(fill=tk.X, padx=10, pady=10)

        self.theme_var = tk.StringVar(value=self.config_manager.config.theme)
        for theme in Theme:
            ttk.Radiobutton(theme_frame, text=theme.value.title(),
                            variable=self.theme_var, value=theme.value,
                            command=self._change_theme).pack(anchor=tk.W, padx=5, pady=2)

        # Other settings
        other_frame = ttk.LabelFrame(parent, text="Other Settings")
        other_frame.pack(fill=tk.X, padx=10, pady=10)

        self.keychain_var = tk.BooleanVar(value=self.config_manager.config.use_keychain)
        ttk.Checkbutton(other_frame, text=self.i18n["use_keychain"],
                        variable=self.keychain_var,
                        command=self._save_settings).pack(anchor=tk.W, padx=5, pady=2)

        # Save button
        ttk.Button(parent, text="Save Settings",
                   command=self._save_settings).pack(pady=20)

    def _update_action_list(self):
        """Update the action combobox."""
        if self.show_advanced.get():
            actions = self.registry.get_all_actions()
        else:
            actions = self.registry.get_basic_actions()

        # Group by category
        action_names = []
        categories = {}
        for action in actions:
            if action.category not in categories:
                categories[action.category] = []
            categories[action.category].append(action.label)

        # Build flat list with category headers
        for category, items in categories.items():
            action_names.append(f"--- {category.title()} ---")
            action_names.extend(sorted(items))

        self.action_combobox['values'] = action_names

    def _on_action_select(self, event):
        """Handle action selection."""
        selection = self.action_combobox.get()
        if selection.startswith("---"):
            return

        # Find action by label
        action = None
        for a in self.registry.get_all_actions():
            if a.label == selection:
                action = a
                break

        if action:
            self._setup_parameter_widgets(action)

    def _setup_parameter_widgets(self, action: ActionDefinition):
        """Setup parameter input widgets for action."""
        # Clear existing widgets
        for widget in self.param_frame.winfo_children():
            widget.destroy()
        self.param_widgets.clear()

        if not action.parameters:
            ttk.Label(self.param_frame, text="No parameters required").pack(pady=20)
            return

        # Create widgets for each parameter
        for i, param in enumerate(action.parameters):
            row_frame = ttk.Frame(self.param_frame)
            row_frame.pack(fill=tk.X, padx=5, pady=5)

            # Label
            label_text = param.label
            if param.required:
                label_text += " *"
            ttk.Label(row_frame, text=label_text, width=20).pack(side=tk.LEFT)

            # Input widget based on parameter type
            if param.type == "bool":
                var = tk.BooleanVar(value=param.default or False)
                widget = ttk.Checkbutton(row_frame, variable=var)
                self.param_widgets[param.name] = var
            elif param.type == "choice":
                var = tk.StringVar(value=param.default or (param.choices[0] if param.choices else ""))
                widget = ttk.Combobox(row_frame, textvariable=var, values=param.choices, state="readonly")
                self.param_widgets[param.name] = var
            elif param.type == "int":
                var = tk.StringVar(value=str(param.default) if param.default is not None else "")
                widget = ttk.Entry(row_frame, textvariable=var)
                self.param_widgets[param.name] = var
            elif param.type == "path":
                var = tk.StringVar(value=param.default or "")
                widget = ttk.Entry(row_frame, textvariable=var)

                # Add browse button
                ttk.Button(row_frame, text="Browse...",
                           command=lambda p=param, v=var: self._browse_path(p, v)).pack(side=tk.RIGHT, padx=5)

                self.param_widgets[param.name] = var
            else:  # string, url
                var = tk.StringVar(value=param.default or "")
                widget = ttk.Entry(row_frame, textvariable=var)
                self.param_widgets[param.name] = var
            # Pack the input widget into the row
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

            # Add help tooltip if description exists
            if param.description:
                self._create_tooltip(widget, param.description)

    def _browse_path(self, param: ActionParameter, var: tk.StringVar):
        """Browse for file or directory path."""
        if param.type == "path":
            if param.name.lower() in ["directory", "dest", "wc", "working_copy", "branch_path", "trunk_path"]:
                path = filedialog.askdirectory()
            else:
                filetypes = param.file_types or [("All files", "*.*")]
                path = filedialog.askopenfilename(filetypes=filetypes)

            if path:
                var.set(path)

    def _show_login_dialog(self) -> bool:
        """
        Show a modal login dialog requesting repository URL and SVN credentials.
        Returns True on successful authentication, False if the user cancelled.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("SVN Login")
        dlg.transient(self.root)
        dlg.grab_set()
        # Slightly larger default size and allow horizontal resizing so the dialog
        # isn't clipped on smaller screens or with larger fonts.
        dlg.geometry("560x260")
        dlg.resizable(True, False)

        # Variables
        url_var = tk.StringVar(value="")
        user_var = tk.StringVar(value="")
        pass_var = tk.StringVar(value="")
        remember_var = tk.BooleanVar(value=False)

        # Layout
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        # Make the second column (inputs) expand when the dialog is resized
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Repository URL:").grid(row=0, column=0, sticky=tk.W, pady=(0, 6))
        url_entry = ttk.Entry(frm, textvariable=url_var)
        url_entry.grid(row=0, column=1, pady=(0, 6), sticky=tk.EW)

        ttk.Label(frm, text="Username:").grid(row=1, column=0, sticky=tk.W, pady=(0, 6))
        user_entry = ttk.Entry(frm, textvariable=user_var)
        user_entry.grid(row=1, column=1, pady=(0, 6), sticky=tk.EW)

        ttk.Label(frm, text="Password:").grid(row=2, column=0, sticky=tk.W, pady=(0, 6))
        pass_entry = ttk.Entry(frm, textvariable=pass_var, show="*")
        pass_entry.grid(row=2, column=1, pady=(0, 6), sticky=tk.EW)

        ttk.Checkbutton(frm, text="Remember credentials (macOS Keychain)",
                        variable=remember_var).grid(row=3, column=1, sticky=tk.W, pady=(0, 6))

        status_label = ttk.Label(frm, text="", foreground="red")
        status_label.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(12, 0))
        btn_frame.columnconfigure(0, weight=1)

        def on_ok():
            url = url_var.get().strip()
            username = user_var.get().strip()
            password = pass_var.get()
            if not url:
                messagebox.showerror(self.i18n["error"], "Repository URL is required", parent=dlg)
                return

            # Show busy cursor
            dlg.config(cursor="watch")
            dlg.update_idletasks()

            try:
                success, message = self._attempt_login(url, username, password)
            finally:
                dlg.config(cursor="")
                dlg.update_idletasks()

            if success:
                # Optionally store credentials in keychain
                if remember_var.get() and username and password:
                    try:
                        # Use service name derived from URL
                        service = url
                        self.svn.credential_store.set_credential(service, username, password)
                    except Exception:
                        # Non-fatal; just log
                        logging.debug("Failed to store credentials in keychain")

                dlg.grab_release()
                dlg.destroy()
                self.status_var.set(f"Connected to {url}")
            else:
                # Keep dialog open and show message
                status_label.config(text=message or "Authentication failed")
                # Also show a messagebox for emphasis
                messagebox.showerror(self.i18n["error"], message or "Failed to connect or authenticate", parent=dlg)

        def on_cancel():
            if messagebox.askyesno("Cancel", "Cancel login and quit application?", parent=dlg):
                dlg.grab_release()
                dlg.destroy()

        ok_btn = ttk.Button(btn_frame, text="Connect", command=on_ok)
        ok_btn.pack(side=tk.RIGHT, padx=6)
        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=on_cancel)
        cancel_btn.pack(side=tk.RIGHT)

        # Focus
        url_entry.focus_set()

        # Wait for dialog to be dismissed; after it's destroyed check if we have a connection
        self.root.wait_window(dlg)

        # Determine if login succeeded by checking status_var (set on success) or by trying to
        # detect saved working state. Simpler: if status_var contains "Connected to", treat as success.
        return bool(self.status_var.get().startswith("Connected to"))

    def _attempt_login(self, url: str, username: str, password: str) -> (bool, Optional[str]):
        """
        Attempt to contact the repository URL and authenticate (if username provided).
        Returns (success: bool, message: Optional[str]).
        """
        # Build svn info args. Use --trust-server-cert for HTTPS servers that need it.
        args = ["info", url]

        # Add credentials if provided
        if username:
            args.extend(["--username", username])
        if password:
            args.extend(["--password", password])

        # Some servers present untrusted certs; add trust flag - best-effort
        args.extend(["--trust-server-cert"])

        # Run the command
        try:
            result = self.svn.run(args)
        except Exception as e:
            logging.debug(f"Login attempt exception: {e}")
            return False, f"Failed to run SVN: {e}"

        if result.exit_code == 0:
            return True, None

        # Analyze stderr for common failure reasons
        stderr = (result.stderr or "").lower()
        if "authorization failed" in stderr or "authentication failed" in stderr or "403" in stderr:
            return False, "Authentication failed: invalid username/password"
        if "e170013" in stderr or "could not connect to server" in stderr or "unable to connect" in stderr:
            return False, "Connection failed: unable to reach repository URL"
        if "certificate" in stderr or "ssl" in stderr:
            # Inform user about certificate issues and suggest options
            return False, "SSL/certificate issue when contacting repository (server certificate may be untrusted)"
        # Generic error
        return False, result.stderr.strip()[:800] if result.stderr else "Unknown error contacting repository"

    def _create_tooltip(self, widget: tk.Widget, text: str):
        """Create a simple tooltip for a widget."""
        def show_tooltip(event):
            tooltip = tk.Toplevel()
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            label = ttk.Label(tooltip, text=text, background="lightyellow",
                              relief="solid", borderwidth=1)
            label.pack()
            widget.tooltip = tooltip

        def hide_tooltip(event):
            if hasattr(widget, 'tooltip'):
                widget.tooltip.destroy()
                delattr(widget, 'tooltip')

        widget.bind("<Enter>", show_tooltip)
        widget.bind("<Leave>", hide_tooltip)

    def _execute_action(self):
        """Execute the selected action."""
        selection = self.action_combobox.get()
        if not selection or selection.startswith("---"):
            messagebox.showwarning(self.i18n["warning"], "Please select an action")
            return

        # Find selected working copy
        if not self.selected_wc.get():
            messagebox.showwarning(self.i18n["warning"], self.i18n["no_wc_selected"])
            return

        wc_path = self.selected_wc.get()

        # Check if working copy is busy
        if self.job_manager.is_job_active(wc_path):
            messagebox.showwarning(self.i18n["warning"], self.i18n["wc_busy"])
            return

        # Find action definition
        action = None
        for a in self.registry.get_all_actions():
            if a.label == selection:
                action = a
                break

        if not action:
            messagebox.showerror(self.i18n["error"], f"Action not found: {selection}")
            return

        # Collect parameters
        params = self._collect_parameters(action)
        if params is None:  # Validation failed
            return

        # Start background execution
        self._start_background_action(action, wc_path, params)

    def _collect_parameters(self, action: ActionDefinition) -> Optional[Dict[str, Any]]:
        """Collect and validate parameter values."""
        params = {}

        for param in action.parameters:
            if param.name not in self.param_widgets:
                continue

            widget_var = self.param_widgets[param.name]

            if param.type == "bool":
                value = widget_var.get()
            else:
                value = widget_var.get().strip()

            # Validate required parameters
            if param.required and not value:
                messagebox.showerror(self.i18n["error"],
                                     f"Required parameter '{param.label}' is missing")
                return None

            # Type validation
            if value:
                if param.type == "int":
                    try:
                        value = int(value)
                    except ValueError:
                        messagebox.showerror(self.i18n["error"],
                                             f"Parameter '{param.label}' must be a number")
                        return None
                elif param.type == "path" and not Path(value).exists():
                    # Only warn for paths, don't fail validation
                    if messagebox.askyesno("Path Warning",
                                           f"Path '{value}' does not exist. Continue anyway?"):
                        pass
                    else:
                        return None

            if value is not None and value != "":
                params[param.name] = value

        return params

    def _start_background_action(self, action: ActionDefinition, wc_path: str,
                                 params: Dict[str, Any]):
        """Start action execution in background thread."""
        if not self.job_manager.start_job(wc_path):
            return

        # Update UI
        self.status_var.set(f"Running {action.label}...")
        self.progress_var.set("In Progress")

        # Start thread
        thread = threading.Thread(
            target=self._execute_action_background,
            args=(action, wc_path, params),
            daemon=True
        )
        thread.start()

    def _execute_action_background(self, action: ActionDefinition, wc_path: str,
                                   params: Dict[str, Any]):
        """Execute action in background thread."""
        try:
            # Add working copy path if not specified and action needs it
            if "path" in [p.name for p in action.parameters] and "path" not in params:
                params["path"] = wc_path

            # Execute action
            if action.composite:
                result = self._execute_workflow_action(action.id, params)
            else:
                result = self._execute_svn_action(action, params)

            # Send result to UI thread
            self.message_queue.put(("action_complete", action, result))

        except Exception as e:
            logging.error(f"Action execution failed: {e}")
            error_result = SvnResult("", str(e), 1, 0, action.id)
            self.message_queue.put(("action_error", action, error_result))

        finally:
            self.job_manager.finish_job(wc_path)

    def _execute_workflow_action(self, workflow_id: str, params: Dict[str, Any]) -> SvnResult:
        """Execute a workflow action."""
        if workflow_id == "branch-create":
            return self.workflows.execute_branch_create(params)
        elif workflow_id == "branch-sync":
            return self.workflows.execute_branch_sync(params)
        elif workflow_id == "branch-merge-to-trunk":
            return self.workflows.execute_branch_merge_to_trunk(params)
        elif workflow_id == "tag-create":
            return self.workflows.execute_tag_create(params)
        else:
            raise ValueError(f"Unknown workflow: {workflow_id}")

    def _execute_svn_action(self, action: ActionDefinition, params: Dict[str, Any]) -> SvnResult:
        """Execute a regular SVN action."""
        args = [action.id]

        # Convert parameters to SVN command line arguments
        for param in action.parameters:
            if param.name in params:
                value = params[param.name]
                if isinstance(value, bool):
                    if value:
                        args.append(f"--{param.name}")
                else:
                    args.extend([f"--{param.name}", str(value)])

        return self.svn.run(args)

    def _cancel_action(self):
        """Cancel the currently running action."""
        if self.selected_wc.get():
            self.job_manager.cancel_job(self.selected_wc.get())
            self.status_var.set("Action cancelled")
            self.progress_var.set("Cancelled")

    def _process_message_queue(self):
        """Process messages from background threads."""
        try:
            while True:
                message_type, *args = self.message_queue.get_nowait()

                if message_type == "action_complete":
                    action, result = args
                    self._handle_action_complete(action, result)
                elif message_type == "action_error":
                    action, result = args
                    self._handle_action_error(action, result)
                elif message_type == "log_message":
                    message = args[0]
                    self._log_message(message)

        except queue.Empty:
            pass

        # Schedule next check
        self.root.after(100, self._process_message_queue)

    def _handle_action_complete(self, action: ActionDefinition, result: SvnResult):
        """Handle successful action completion."""
        self.status_var.set(f"{action.label} completed")
        self.progress_var.set(f"Done ({result.elapsed:.1f}s)")

        # Log the result
        self._log_message(f"✓ {action.label} completed successfully")
        if result.stdout:
            self._log_message(result.stdout)

        # Refresh working copy status and update Finder indicator
        if self.selected_wc.get():
            self._refresh_working_copy_status(self.selected_wc.get())

    def _handle_action_error(self, action: ActionDefinition, result: SvnResult):
        """Handle action execution error."""
        self.status_var.set(f"{action.label} failed")
        self.progress_var.set("Failed")

        # Log the error
        self._log_message(f"✗ {action.label} failed")
        if result.stderr:
            self._log_message(f"Error: {result.stderr}")

        # Show error dialog
        messagebox.showerror(
            self.i18n["error"],
            f"{action.label} failed:\n{result.stderr[:200]}..."
        )

    def _log_message(self, message: str):
        """Add a message to the activity log."""
        if self.log_text:
            timestamp = datetime.now().strftime("%H:%M:%S")
            formatted_message = f"[{timestamp}] {message}\n"

            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, formatted_message)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

    def _on_wc_select(self, event):
        """Handle working copy selection."""
        selection = self.wc_listbox.curselection()
        if selection:
            index = selection[0]
            wc_display = self.wc_listbox.get(index)
            # wc_display is like "name (path)" — extract path inside parentheses
            m = re.search(r"\((.*)\)$", wc_display)
            if m:
                wc_path = m.group(1)
            else:
                wc_path = wc_display
            self.selected_wc.set(wc_path)

            # Update repository browser
            self._update_repo_browser(wc_path)

    def _add_working_copy(self):
        """Add a new working copy."""
        path = filedialog.askdirectory(title=self.i18n["choose_directory"])
        if not path:
            return

        # Check if it's a valid SVN working copy
        if not self.svn.is_working_copy(path):
            messagebox.showerror(
                self.i18n["error"],
                f"'{path}' is not a valid SVN working copy"
            )
            return

        # Get working copy info
        wc_info = self.svn.get_working_copy_info(path)
        if not wc_info:
            messagebox.showerror(
                self.i18n["error"],
                "Failed to get working copy information"
            )
            return

        # Add to configuration
        self.config_manager.config.working_copies.append({
            "path": path,
            "url": wc_info.url,
            "name": Path(path).name
        })
        self.config_manager.save()

        # Update UI
        self._refresh_working_copies()
        # Update Finder indicator for newly added wc
        self._update_finder_indicator_for_path(path)

    def _remove_working_copy(self):
        """Remove selected working copy."""
        if not self.selected_wc.get():
            return

        wc_path = self.selected_wc.get()

        if messagebox.askyesno("Confirm", f"Remove working copy '{wc_path}'?"):
            # Remove from config
            self.config_manager.config.working_copies = [
                wc for wc in self.config_manager.config.working_copies
                if wc["path"] != wc_path
            ]
            self.config_manager.save()

            # Clear Finder indicator comment when removed (best-effort)
            self._clear_finder_tag(wc_path)

            # Update UI
            self.selected_wc.set("")
            self._refresh_working_copies()

    def _refresh_working_copies(self):
        """Refresh the working copies list."""
        self.wc_listbox.delete(0, tk.END)

        for wc in self.config_manager.config.working_copies:
            display_name = f"{wc['name']} ({wc['path']})"
            self.wc_listbox.insert(tk.END, display_name)

            # Store the actual path for selection
            if wc["path"] not in self.working_copies:
                wc_info = self.svn.get_working_copy_info(wc["path"])
                if wc_info:
                    self.working_copies[wc["path"]] = wc_info
            # Always update finder indicator (ensure status is up to date)
            self._update_finder_indicator_for_path(wc["path"])

    def _discover_working_copies(self):
        """Discover working copies automatically."""
        if not self.config_manager.config.working_copies:
            # Look for .svn directories in common locations
            search_paths = [
                Path.home() / "Documents",
                Path.home() / "Desktop",
                Path.cwd()
            ]

            found_wcs = []
            for search_path in search_paths:
                if search_path.exists():
                    for path in search_path.rglob(".svn"):
                        wc_path = str(path.parent)
                        if self.svn.is_working_copy(wc_path):
                            wc_info = self.svn.get_working_copy_info(wc_path)
                            if wc_info:
                                found_wcs.append({
                                    "path": wc_path,
                                    "url": wc_info.url,
                                    "name": Path(wc_path).name
                                })

                                if len(found_wcs) >= 10:  # Limit discovery
                                    break

            if found_wcs:
                self.config_manager.config.working_copies.extend(found_wcs)
                self.config_manager.save()

        self._refresh_working_copies()

    def _update_repo_browser(self, wc_path: str):
        """Update repository browser with working copy info."""
        wc_info = self.working_copies.get(wc_path)
        info_text = ""
        if wc_info:
            info_text = (
                f"URL: {wc_info.url}\n"
                f"Repository Root: {wc_info.repository_root}\n"
                f"Revision: {wc_info.revision}\n"
                f"Last Changed: {wc_info.last_changed_rev}\n"
                f"UUID: {wc_info.uuid[:8] if wc_info.uuid else ''}"
            )

        self.repo_info_text.config(state=tk.NORMAL)
        self.repo_info_text.delete(1.0, tk.END)
        self.repo_info_text.insert(1.0, info_text)
        self.repo_info_text.config(state=tk.DISABLED)

    # ---------------------------
    # Finder indicator (macOS) - methods bound to GUI instance
    # ---------------------------
    def _run_osascript(self, script: str) -> bool:
        """Run an AppleScript via osascript; return True on success."""
        if platform.system() != "Darwin":
            return False
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            logging.debug(f"osascript failed: {e}")
            return False

    def _set_finder_tag(self, path: str, tag_name: str) -> bool:
        """
        Add a Finder tag (label) to the file/folder at path.
        tag_name should be an existing Finder tag (e.g. "Red", "Green") or a custom tag name.
        This uses AppleScript to modify the tags metadata visible in Finder.
        """
        if platform.system() != "Darwin":
            return False

        # Escape single quotes in path and tag_name
        safe_path = path.replace("'", "\\'")
        safe_tag = tag_name.replace("'", "\\'")
        script = (
            f"try\n"
            f"  set theFile to POSIX file '{safe_path}' as alias\n"
            f"  tell application \"Finder\"\n"
            f"    set currentTags to name of every tag of theFile\n"
            f"    if currentTags does not contain \"{safe_tag}\" then\n"
            f"      make new tag at theFile with properties {{name:\"{safe_tag}\"}}\n"
            f"    end if\n"
            f"  end tell\n"
            f"  return true\n"
            f"on error errMsg\n"
            f"  return false\n"
            f"end try"
        )
        return self._run_osascript(script)

    def _clear_finder_tag(self, path: str, tag_name: Optional[str] = None) -> bool:
        """
        Remove a Finder tag from the file/folder at path.
        If tag_name is None, attempt to remove our 'SVNMVP:Dirty'/'SVNMVP:Clean' tags
        and common color tags.
        """
        if platform.system() != "Darwin":
            return False

        safe_path = path.replace("'", "\\'")
        if tag_name:
            safe_tag = tag_name.replace("'", "\\'")
            script = (
                f"try\n"
                f"  set theFile to POSIX file '{safe_path}' as alias\n"
                f"  tell application \"Finder\"\n"
                f"    set currentTags to every tag of theFile\n"
                f"    repeat with t in currentTags\n"
                f"      if name of t is \"{safe_tag}\" then\n"
                f"        delete t\n"
                f"        exit repeat\n"
                f"      end if\n"
                f"    end repeat\n"
                f"  end tell\n"
                f"  return true\n"
                f"on error\n"
                f"  return false\n"
                f"end try"
            )
            return self._run_osascript(script)
        else:
            # Try removing common tags we might set
            removed_any = False
            for tag in ("SVNMVP:Dirty", "SVNMVP:Clean", "Red", "Green"):
                try:
                    if self._clear_finder_tag(path, tag):
                        removed_any = True
                except Exception:
                    pass
            return removed_any

    def _update_finder_indicator_for_path(self, path: str):
        """
        Check WC status and set Finder tag:
         - 'SVNMVP:Dirty' (or Red) if there are uncommitted changes
         - 'SVNMVP:Clean' (or Green) if clean
        This is best-effort and will silently no-op on non-macOS.
        """
        try:
            if platform.system() != "Darwin":
                return

            has_changes = self.svn.has_uncommitted_changes(path)
            if has_changes:
                # Remove clean tag, set dirty tag
                self._clear_finder_tag(path, "SVNMVP:Clean")
                # Prefer setting a colored system tag if available, else use named tag
                if not self._set_finder_tag(path, "Red"):
                    self._set_finder_tag(path, "SVNMVP:Dirty")
            else:
                # Remove dirty tag, set clean tag
                self._clear_finder_tag(path, "SVNMVP:Dirty")
                if not self._set_finder_tag(path, "Green"):
                    self._set_finder_tag(path, "SVNMVP:Clean")
        except Exception as e:
            logging.debug(f"Failed to update Finder indicator for {path}: {e}")

    def _browse_repo_path(self, path_type: str):
        """Browse repository path in external browser or copy to clipboard."""
        if not self.selected_wc.get():
            messagebox.showwarning(self.i18n["warning"], self.i18n["no_wc_selected"])
            return

        wc_info = self.working_copies.get(self.selected_wc.get())
        if not wc_info:
            return

        # Construct URL based on standard layout
        repo_root = wc_info.repository_root
        if path_type == "trunk":
            url = f"{repo_root}/{self.config_manager.config.default_trunk_path}"
        elif path_type == "branches":
            url = f"{repo_root}/{self.config_manager.config.default_branches_path}"
        elif path_type == "tags":
            url = f"{repo_root}/{self.config_manager.config.default_tags_path}"
        else:
            url = repo_root

        # Try to open in web browser (if it's HTTP/HTTPS)
        if url.startswith(("http://", "https://")):
            webbrowser.open(url)
        else:
            # Copy URL to clipboard
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(url)
                messagebox.showinfo("URL Copied", f"Repository URL copied to clipboard:\n{url}")
            except Exception:
                # Fallback to showing in dialog
                self._show_text_dialog("Repository URL", url)

    def _open_repo_listing(self, url: str = None, revision: Optional[str] = None,
                           recursive: bool = False):
        """
        Open a repository listing for the given URL.
        If url is None and a working copy is selected, derive the repo root and use it.
        """
        if not url:
            wc_info = self.working_copies.get(self.selected_wc.get())
            if not wc_info:
                messagebox.showwarning(self.i18n["warning"], self.i18n["no_wc_selected"])
                return
            url = wc_info.repository_root

        result = self.svn.list_url(url, revision=revision, verbose=True, recursive=recursive)
        if result.exit_code != 0:
            messagebox.showerror(self.i18n["error"], f"Failed to list {url}:\n{result.stderr}")
            return

        # Show raw output in a dialog; could be parsed into a tree later
        self._show_text_dialog(f"Listing: {url}", result.stdout)

    def _open_log_viewer(self, path_or_url: str = None, revision: Optional[str] = None,
                         limit: int = 200, verbose: bool = False, stop_on_copy: bool = False):
        """
        Open an SVN log viewer (parses XML from svn log --xml and displays it).
        If path_or_url is None and a working copy is selected, use that wc's URL.
        """
        if not path_or_url:
            wc_info = self.working_copies.get(self.selected_wc.get())
            if not wc_info:
                messagebox.showwarning(self.i18n["warning"], self.i18n["no_wc_selected"])
                return
            path_or_url = wc_info.url

        result = self.svn.get_log(path_or_url, revision=revision,
                                  limit=limit, verbose=verbose, stop_on_copy=stop_on_copy)
        if result.exit_code != 0:
            messagebox.showerror(self.i18n["error"], f"Failed to get log for {path_or_url}:\n{result.stderr}")
            return

        # Parse XML log output
        try:
            root = ET.fromstring(result.stdout)
        except Exception:
            # Fallback: show raw output if parsing fails
            self._show_text_dialog(f"Log: {path_or_url}", result.stdout)
            return

        entries = []
        for logentry in root.findall('logentry'):
            rev = logentry.get('revision', '?')
            author = (logentry.findtext('author') or '').strip()
            date = (logentry.findtext('date') or '').strip()
            msg = (logentry.findtext('msg') or '').strip()
            header = f"r{rev} | {author} | {date}"
            entries.append(header)
            entries.append(msg)
            # If verbose include changed paths
            if verbose:
                changed = logentry.find('paths')
                if changed is not None:
                    for p in changed.findall('path'):
                        action = p.get('action', '?')
                        ptext = (p.text or '').strip()
                        entries.append(f"  {action} {ptext}")
            entries.append("-" * 72)

        text = "\n".join(entries) if entries else "No log entries found."
        self._show_text_dialog(f"Log: {path_or_url}", text)

    def _show_text_dialog(self, title: str, text: str):
        """Helper: show a read-only scrollable dialog with text."""
        try:
            dlg = tk.Toplevel(self.root)
            dlg.title(title)
            dlg.geometry("900x600")
            frame = tk.Frame(dlg)
            frame.pack(fill=tk.BOTH, expand=True)
            st = scrolledtext.ScrolledText(frame, wrap=tk.WORD)
            st.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
            st.insert("1.0", text)
            st.configure(state="disabled")

            btn_frame = tk.Frame(dlg)
            btn_frame.pack(fill=tk.X, pady=(0, 6))
            tk.Button(btn_frame, text=self.i18n.get("close", "Close"), command=dlg.destroy).pack(side=tk.RIGHT, padx=6)
        except Exception as e:
            logging.debug(f"Failed to open text dialog: {e}")
            # Last resort: print to log_text or console
            self._log_message(text)

    def _refresh_working_copy_status(self, wc_path: str):
        """Refresh status of a working copy."""
        if wc_path in self.working_copies:
            wc_info = self.svn.get_working_copy_info(wc_path)
            if wc_info:
                self.working_copies[wc_path] = wc_info
                if wc_path == self.selected_wc.get():
                    self._update_repo_browser(wc_path)
        # Update Finder indicator after refreshing status
        self._update_finder_indicator_for_path(wc_path)

    def _clear_log(self):
        """Clear the activity log."""
        if not self.log_text:
            return
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _save_log(self):
        """Save the activity log to a file."""
        try:
            filename = filedialog.asksaveasfilename(
                title="Save Log",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )

            if filename:
                content = self.log_text.get(1.0, tk.END)
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(content)
                messagebox.showinfo(self.i18n["success"], f"Log saved to {filename}")
        except Exception as e:
            messagebox.showerror(self.i18n["error"], f"Failed to save log: {e}")

    def _browse_svn_binary(self):
        """Browse for SVN binary path."""
        filename = filedialog.askopenfilename(
            title="Select SVN Binary",
            filetypes=[("Executable files", "*"), ("All files", "*.*")]
        )

        if filename:
            self.binary_var.set(filename)

    def _change_theme(self):
        """Change the application theme."""
        theme_name = self.theme_var.get()
        try:
            theme = Theme(theme_name)
            self.current_theme = theme
            self._apply_theme()

            # Save to config
            self.config_manager.config.theme = theme_name
            self.config_manager.save()

        except Exception:
            messagebox.showerror(self.i18n["error"], f"Invalid theme: {theme_name}")

    def _apply_theme(self):
        """Apply the current theme to all widgets."""
        try:
            if self.current_theme == Theme.SYSTEM:
                # Use system theme detection
                self._detect_system_theme()

            colors = THEMES.get(self.current_theme, THEMES[Theme.LIGHT])

            # Apply to root window
            self.root.configure(bg=colors.bg)

            # Apply to ttk widgets
            style = ttk.Style()
            # Don't assume 'clam' is available/configured in all environments; attempt safe usage
            try:
                style.theme_use('clam')
            except Exception:
                pass

            # Configure ttk styles (best-effort)
            style.configure('TLabel', background=colors.bg, foreground=colors.fg)
            style.configure('TFrame', background=colors.bg)
            style.configure('TButton', background=colors.button_bg, foreground=colors.button_fg)
            style.configure('TEntry', fieldbackground=colors.entry_bg, foreground=colors.entry_fg)
            style.configure('TCombobox', fieldbackground=colors.entry_bg, foreground=colors.entry_fg)
        except Exception as e:
            logging.warning(f"Failed to apply theme: {e}")

    def _save_settings(self):
        """Save current settings."""
        # Update config from UI
        self.config_manager.config.svn_binary_path = self.binary_var.get()
        self.config_manager.config.theme = self.theme_var.get()
        self.config_manager.config.use_keychain = self.keychain_var.get()

        # Validate SVN binary
        try:
            test_svn = SvnRunner(self.config_manager.config.svn_binary_path)
            self.svn = test_svn
            messagebox.showinfo(self.i18n["success"], "Settings saved successfully")
        except ValueError:
            messagebox.showerror(
                self.i18n["error"],
                f"Invalid SVN binary: {self.config_manager.config.svn_binary_path}"
            )
            return

        # Save to file
        self.config_manager.save()

    def _on_closing(self):
        """Handle application closing."""
        # Save window geometry
        try:
            self.config_manager.config.window_geometry = self.root.geometry()
        except Exception:
            pass
        self.config_manager.save()

        # Cleanup any active jobs
        for wc_path in list(self.job_manager.active_jobs.keys()):
            self.job_manager.cancel_job(wc_path)

        try:
            self.root.destroy()
        except Exception:
            pass


def main() -> int:
    """Main entry point."""
    cli = CLI()
    return cli.run()


if __name__ == "__main__":
    sys.exit(main())