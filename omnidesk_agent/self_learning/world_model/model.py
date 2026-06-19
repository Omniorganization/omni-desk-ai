from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class WorldEntity:
    entity_id: str
    entity_type: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorldRelation:
    source_id: str
    relation: str
    target_id: str
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StateTransition:
    entity_id: str
    from_state: str
    to_state: str
    trigger: str
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorldModel:
    """Learns environment entities, relations, states, transitions and predictions."""

    def __init__(self):
        self.entities: dict[str, WorldEntity] = {}
        self.relations: list[WorldRelation] = []
        self.transitions: list[StateTransition] = []
        self.current_state: dict[str, str] = {}

    def observe_entity(self, entity_id: str, entity_type: str, **attributes: Any) -> WorldEntity:
        if not entity_id:
            raise ValueError("entity_id is required")
        existing = self.entities.get(entity_id)
        if existing:
            existing.attributes.update(attributes)
            if entity_type:
                existing.entity_type = entity_type
            return existing
        entity = WorldEntity(entity_id, entity_type or "unknown", dict(attributes))
        self.entities[entity_id] = entity
        return entity

    def observe_relation(self, source_id: str, relation: str, target_id: str, *, confidence: float = 1.0) -> WorldRelation:
        if source_id not in self.entities or target_id not in self.entities:
            raise ValueError("both relation endpoints must be observed entities")
        rel = WorldRelation(source_id, relation, target_id, max(0.0, min(float(confidence), 1.0)))
        self.relations.append(rel)
        return rel

    def transition_state(self, entity_id: str, to_state: str, *, trigger: str, confidence: float = 1.0) -> StateTransition:
        if entity_id not in self.entities:
            raise ValueError("entity must be observed before state transitions")
        from_state = self.current_state.get(entity_id, "unknown")
        transition = StateTransition(entity_id, from_state, to_state, trigger, max(0.0, min(float(confidence), 1.0)))
        self.transitions.append(transition)
        self.current_state[entity_id] = to_state
        return transition

    def predict_next_state(self, entity_id: str, *, trigger: str) -> Optional[dict[str, Any]]:
        candidates = [t for t in self.transitions if t.entity_id == entity_id and t.trigger == trigger]
        if not candidates:
            return None
        scores: dict[str, float] = {}
        for item in candidates:
            scores[item.to_state] = scores.get(item.to_state, 0.0) + item.confidence
        state, score = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[0]
        return {"entity_id": entity_id, "trigger": trigger, "predicted_state": state, "confidence": round(min(score / len(candidates), 1.0), 4)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "entities": {key: entity.to_dict() for key, entity in sorted(self.entities.items())},
            "relations": [relation.to_dict() for relation in self.relations],
            "current_state": dict(sorted(self.current_state.items())),
            "transitions": [transition.to_dict() for transition in self.transitions],
        }
