
'Shared context: everything a step needs to know and record.\n\nA *step* is a function ``fn(ctx) -> R | None``. It decides whether there is work to do\n(``skipped(reason)``), performs work using ``ctx.cmd`` (changes the system) and ``ctx.read_only_command``\n(read-only), and records relevant information through ``ctx.warning/error/note/issue``.\n'
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .environment import Environment, ENV_APT, APT_OPTIONS
from .storage import SpaceMeter
from .executor import Cmd, Executor, StopSignal, short_label
from .logger import LogRecorder

OK, SKIPPED, WARNING, ERROR = "ok", "skipped", "warning", "error"


REVERSIBLE = 'reversible'
PARTIAL = 'partially reversible'
DIFFICULT = 'difficult to undo'


class R:
    __slots__ = ("status", "reason")

    def __init__(self, status: str = OK, reason: str = ""):
        self.status = status
        self.reason = reason

    def __repr__(self) -> str:  
        return f"R({self.status!r}, {self.reason!r})"


def ok(reason: str = "") -> R:
    return R(OK, reason)


def skipped(reason: str) -> R:
    return R(SKIPPED, reason)


def warning(reason: str = "") -> R:
    return R(WARNING, reason)


def error(reason: str = "") -> R:
    return R(ERROR, reason)


@dataclass
class Step:
    id: str
    title: str
    phase: str                      
    fn: Callable[["Context"], Optional[R]]
    weight: int = 1                  
    category: str = ""            
    reversibility: str = PARTIAL
    root: bool = False             


@dataclass
class StepRecord:
    id: str
    title: str
    phase: str
    round: int
    status: str
    reason: str
    duration: float
    delta: int                     
    category: str
    reversibility: str
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    declared: int = 0             


@dataclass
class Issue:
    category: str
    description: str
    state: str                    
    key: str = ""                


class Context:
    def __init__(self, env_data: Environment, selection: Set[str], ui: Any, log: LogRecorder, *, simulate: bool,
                 has_root: bool, stop_signal: StopSignal):
        self.env_data = env_data
        self.selection = set(selection)                 
        self.ui = ui
        self.log = log
        self.simulate = simulate
        self.has_root = has_root
        self.stop_signal = stop_signal
        self.t0 = time.time()
        self.end_time: Optional[float] = None
        self._step_failures: List[str] = []
        self._simulation_cache: Dict[tuple, Any] = {}
        self.exec = Executor(env_data, log, simulate=simulate, has_root=has_root, stop_signal=stop_signal,
                             on_command=ui.command, on_output=ui.output, on_failure=self._failure)
        self.meter = SpaceMeter(self._df, env_data.home)

        
        self.step: Optional[Step] = None
        self.round = 1

        
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.notes: List[str] = []
        self.issues: List[Issue] = []
        self.procedures: List[str] = []       
        self.not_updated_items: List[Tuple[str, str]] = []
        self.results: List[StepRecord] = []

        
        self.state: Any = None
        self.offline = False
        self.apt_locked = False
        self.apt_index_broken = False
        self.dpkg_error_pending = False
        self.pending_after_update: Optional[int] = None
        self.kernel_updated = False
        self.space_before = None
        self.space_after = None
        self.category_delta: Dict[str, int] = {"update": 0, "repair": 0, "cleanup": 0, "other": 0}
        self.inventory_before: Dict[str, Dict[str, str]] = {}
        self.inventory_after: Dict[str, Dict[str, str]] = {}
        self.firmware: Dict[str, Any] = {}
        self.timeshift: Dict[str, Any] = {}
        self.reboot: Dict[str, Any] = {"level": 0, "reasons": []}
        self.ram: Dict[str, Any] = {}
        self.aborted = ""                        
        self.order = ""                           
        self.ordering_reasons: List[str] = []
        self.completed_rounds = 0
        self.upgrade_error_index = -1                
        self.flatpak_needs_repair = False
        self.dkms_alerts: List[str] = []
        self.autoremove_rejected = False
        self.preserved_rc: Set[str] = set()
        self.ignored_selection: Dict[str, str] = {}       
        self.opportunities: List[Tuple[int, str, str]] = []   
        self.declared = 0                        
        self.cleanup_details: Dict[str, int] = {}
        self.cleanup_counts: Dict[str, int] = {}     

    
    def _df(self, argv: List[str]) -> Tuple[int, str]:
        r = self.exec.run(argv, log_output=False, silent=True)
        return r.return_code, r.out

    def _failure(self, description: str) -> None:
        self._step_failures.append(description)

    def step_title(self) -> str:
        return self.step.title if self.step else "general"

    @property
    def interrupted(self) -> bool:
        return self.stop_signal.requested

    @property
    def stopped(self) -> bool:
        'Stop starting new work: the user requested it, or something made continuing unsafe.'
        return self.stop_signal.requested or bool(self.aborted)

    def has_tool(self, tool: str) -> bool:
        return self.env_data.has_tool(tool)

    
    def cmd(self, argv: List[str], **kw: Any) -> Cmd:
        "Command that CHANGES the system (listed under 'procedures performed')."
        r = self.exec.run(argv, action=True, **kw)
        if argv and argv[0] in ("apt-get", "apt", "dpkg") and not r.simulated:
            self._simulation_cache.clear()        
        if not r.denied:
            self.procedures.append("%s%s [%s]" % ("[root] " if kw.get("root") else "", short_label(argv),
                                                     "dry run" if r.simulated else f"code {r.return_code}"))
        return r

    def read_only_command(self, argv: List[str], **kw: Any) -> Cmd:
        'Read-only command (also runs in dry-run mode).'
        return self.exec.run(argv, action=False, **kw)

    def apt(self, argv: List[str], **kw: Any) -> Cmd:
        'apt-get command that changes the system, with the standard options and environment (v1.3).'
        env = dict(ENV_APT)
        env.update(kw.pop("env", {}) or {})
        return self.cmd(["apt-get", *APT_OPTIONS, *argv], root=True, protected=True, env=env, **kw)

    
    def warning(self, msg: str) -> None:
        self.warnings.append(f"{self.step_title()}: {msg}")
        self.log.write(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        self.errors.append(f"{self.step_title()}: {msg}")
        self.log.write(f"ERROR: {msg}")

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        self.log.write(f"NOTE: {msg}")

    def issue(self, category: str, description: str, state: str, key: str = "") -> None:
        'Records a detected issue. With ``key``, updates the existing state if it is already present.'
        if key:
            for part in self.issues:
                if part.key == key:
                    part.state = state
                    if description:
                        part.description = description
                    self.log.write(f"ISSUE [{state}] {category}: {part.description}")
                    return
        self.issues.append(Issue(category, description, state, key))
        self.log.write(f"ISSUE [{state}] {category}: {description}")

    def not_updated(self, item: str, reason: str) -> None:
        if (item, reason) in self.not_updated_items:
            return
        self.not_updated_items.append((item, reason))
        self.log.write(f"NOT UPDATED: {item} — {reason}")

    def recover_upgrade(self) -> None:
        'If the upgrade failed in dpkg and a repair completed the installation, the error becomes a warning (v1.3).'
        i = self.upgrade_error_index
        if 0 <= i < len(self.errors):
            self.warnings.append(f"{self.errors[i]} (recovered: repair completed the interrupted installation)")
            del self.errors[i]
        self.upgrade_error_index = -1

    def progress(self, frac: Optional[float], text: str = "") -> None:
        self.ui.progress(frac, text)

    
    def free_mb(self, mount_point: str = "/") -> int:
        return self.meter.free_mb(mount_point)

