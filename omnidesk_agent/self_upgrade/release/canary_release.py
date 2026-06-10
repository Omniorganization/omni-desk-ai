from __future__ import annotations
import json, time
from pathlib import Path
class CanaryReleaseManager:
    def __init__(self, state_file: Path): self.state_file=state_file.expanduser(); self.state_file.parent.mkdir(parents=True, exist_ok=True)
    def enable(self, target: str, version: str, allowed_risk: str="low") -> dict:
        state=self._load(); state[target]={"channel":"canary","version":version,"allowed_risk":allowed_risk,"enabled_at":time.time()}; self._save(state); return state[target]
    def disable(self, target: str) -> None:
        state=self._load()
        if target in state: state[target]["channel"]="stable"; state[target]["disabled_at"]=time.time()
        self._save(state)
    def should_use_canary(self, target: str, task_risk: str) -> bool:
        st=self._load().get(target); order={"low":0,"medium":1,"high":2,"critical":3}
        return bool(st and st.get("channel")=="canary" and order.get(task_risk,3)<=order.get(st.get("allowed_risk","low"),0))
    def _load(self):
        return json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
    def _save(self,state): self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
