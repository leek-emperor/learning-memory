"""会话状态机 —— 对应 Claude Code 的 Bootstrap State（极简版）"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
import time


class SessionPhase(Enum):
    """三态会话状态机，对应 Claude Code 的 idle / running / requires_action"""
    IDLE = "idle"
    RUNNING = "running"
    REQUIRES_ACTION = "requires_action"


@dataclass
class SessionState:
    """进程级全局状态（对应 Claude Code 的 Bootstrap State）

    设计原则：所有状态通过 getter/setter 访问，不直接暴露字段。
    Claude Code 源码注释连写三遍 "DO NOT ADD MORE STATE HERE"。
    """
    # ── 会话标识 ──
    _session_id: str = ""
    _start_time: float = field(default_factory=time.time)
    _last_interaction_time: float = field(default_factory=time.time)

    # ── 成本追踪 ──
    _total_cost_usd: float = 0.0
    _total_input_tokens: int = 0
    _total_output_tokens: int = 0

    # ── 状态机 ──
    _phase: SessionPhase = SessionPhase.IDLE

    # ── 工作目录 ──
    _cwd: str = "."

    # ── 模型 ──
    _model: str = "gpt-4o-mini"

    # ── 消息游标（用于记忆提取，记录已处理到哪条消息） ──
    _last_processed_msg_index: int = 0

    # ── 单例 ──
    _instance: Optional["SessionState"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── 会话标识 ──
    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str):
        self._session_id = value

    @property
    def start_time(self) -> float:
        return self._start_time

    @property
    def last_interaction_time(self) -> float:
        return self._last_interaction_time

    def touch_interaction(self):
        """标记交互时间（延迟更新，对应 Claude Code 的 dirty flag 机制）"""
        self._last_interaction_time = time.time()

    # ── 成本追踪 ──
    @property
    def total_cost_usd(self) -> float:
        return self._total_cost_usd

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._total_output_tokens

    def accumulate_usage(self, input_tokens: int, output_tokens: int, cost: float):
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._total_cost_usd += cost

    # ── 状态机 ──
    @property
    def phase(self) -> SessionPhase:
        return self._phase

    def set_phase(self, phase: SessionPhase):
        old = self._phase
        self._phase = phase
        if old != phase:
            print(f"  [状态机] {old.value} → {phase.value}")

    # ── 工作目录 ──
    @property
    def cwd(self) -> str:
        return self._cwd

    @cwd.setter
    def cwd(self, value: str):
        self._cwd = value

    # ── 模型 ──
    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str):
        self._model = value

    # ── 消息游标 ──
    @property
    def last_processed_msg_index(self) -> int:
        return self._last_processed_msg_index

    @last_processed_msg_index.setter
    def last_processed_msg_index(self, value: int):
        self._last_processed_msg_index = value

    def reset(self):
        """重置所有状态（用于测试或新会话）"""
        self._session_id = ""
        self._start_time = time.time()
        self._last_interaction_time = time.time()
        self._total_cost_usd = 0.0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._phase = SessionPhase.IDLE
        self._cwd = "."
        self._last_processed_msg_index = 0


# 全局单例
state = SessionState()
