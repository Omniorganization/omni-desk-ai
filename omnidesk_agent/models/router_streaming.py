from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from omnidesk_agent.models.base import ModelDelta, ModelRequest
from omnidesk_agent.models.provider_errors import classify_provider_error
from omnidesk_agent.models.provider_streaming import stream_provider
from omnidesk_agent.models.router import ModelRouter


class GovernedStreamingRouter:
    """Streaming facade preserving ModelRouter ACL, budget, retry and ledger policy."""

    def __init__(self, router: ModelRouter):
        self.router = router

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]:
        plan = self.router.route_plan(request.task, request.metadata)
        last_error: BaseException | None = None
        attempted: list[str] = []
        for configured_profile in plan.profiles:
            profile = configured_profile
            provider = self.router.providers.get(profile)
            if provider is None:
                last_error = RuntimeError(
                    f"Model profile not configured or disabled: {profile}"
                )
                continue
            if self.router._offline_forbids_profile(profile, provider):
                last_error = RuntimeError(
                    f"offline mode forbids external model profile: {profile}"
                )
                continue
            if self.router._circuit_open(profile, plan):
                last_error = RuntimeError(f"Model profile circuit open: {profile}")
                continue
            budget_action = self.router._check_budget(
                request,
                profile=profile,
                provider=provider,
            )
            if budget_action == "fallback_local":
                profile = "local"
                provider = self.router.providers.get(profile)
                if provider is None:
                    last_error = RuntimeError(
                        "model budget exceeded and local fallback is unavailable"
                    )
                    continue

            for attempt in range(plan.max_retries + 1):
                attempted.append(profile)
                emitted = False
                try:
                    async for delta in self._stream_with_provider(
                        request,
                        profile,
                        provider,
                    ):
                        emitted = emitted or bool(delta.text or delta.reasoning)
                        yield delta
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    info = classify_provider_error(exc)
                    self.router.error_counts[info.category] = (
                        self.router.error_counts.get(info.category, 0) + 1
                    )
                    self.router._record_failure(profile)
                    if emitted:
                        raise RuntimeError(
                            "model stream failed after partial delivery: "
                            f"profile={profile}; category={info.category}"
                        ) from exc
                    if info.retryable and attempt < plan.max_retries:
                        await asyncio.sleep(min(0.2 * (2**attempt), 1.0))
                        continue
                    break

        # Some providers, staging fakes and older compatible endpoints expose a
        # governed complete transport but no native stream transport. Falling back
        # before the first delta preserves availability without risking duplicate
        # visible output. ModelRouter.complete remains responsible for all normal
        # ACL, budget, retry, ledger and cache behavior in this path.
        try:
            response = await self.router.complete(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            tried = ", ".join(attempted or plan.profiles)
            raise RuntimeError(
                "All model profiles failed for streaming "
                f"task={request.task}; tried={tried}; "
                f"native_error={last_error}; fallback_error={exc}"
            ) from exc
        yield ModelDelta(
            sequence=1,
            provider=response.provider,
            model=response.model,
            profile=response.profile,
            text=response.text,
            usage=response.usage or {},
            finish_reason="complete_fallback",
            provider_request_id=str((response.raw or {}).get("id") or "") or None,
            native=False,
        )

    async def _stream_with_provider(
        self,
        request: ModelRequest,
        profile: str,
        provider: Any,
    ) -> AsyncIterator[ModelDelta]:
        decision = self.router.token_budget.decide(
            model=f"{profile}:{provider.model}",
            system=request.system,
            user=request.user,
            task_id=request.task_id,
            expected_output_tokens=getattr(
                provider.settings,
                "max_output_tokens",
                self.router.cfg.max_output_tokens,
            ),
            verified_required=request.verified_required,
        )
        if not decision.allowed:
            raise RuntimeError(
                f"Model call blocked by token guardrail: {decision.reason}"
            )

        cached = self.router.token_budget.get_cached(decision.cache_key or "")
        if cached is not None:
            yield ModelDelta(
                sequence=1,
                provider="cache",
                model=str(provider.model),
                profile=profile,
                text=cached,
                usage={"cache_hit": True},
                finish_reason="cache_hit",
                native=False,
            )
            return

        safe = ModelRequest(
            system=decision.truncated_system or request.system,
            user=decision.truncated_user or request.user,
            task=request.task,
            images=request.images,
            json_mode=request.json_mode,
            verified_required=request.verified_required,
            task_id=request.task_id,
            metadata={**request.metadata, "profile": profile},
        )

        text_parts: list[str] = []
        final_usage: dict[str, Any] = {}
        final_reason = "stop"
        provider_request_id: str | None = None
        native = True
        final_sequence = 1
        async for delta in stream_provider(provider, safe):
            final_sequence = max(final_sequence, int(delta.sequence))
            provider_request_id = delta.provider_request_id or provider_request_id
            native = native and bool(delta.native)
            if delta.text:
                text_parts.append(delta.text)
            if delta.usage:
                final_usage.update(delta.usage)
            if delta.finish_reason:
                final_reason = delta.finish_reason
            if delta.text or delta.reasoning:
                yield ModelDelta(
                    sequence=delta.sequence,
                    provider=str(
                        getattr(provider, "provider_name", delta.provider)
                    ),
                    model=str(provider.model),
                    profile=profile,
                    text=delta.text,
                    reasoning=delta.reasoning,
                    provider_request_id=provider_request_id,
                    native=delta.native,
                )

        if final_reason in {"failed", "incomplete"}:
            raise RuntimeError(
                "provider stream terminated without a successful completion: "
                f"profile={profile}; finish_reason={final_reason}; "
                f"provider_request_id={provider_request_id or 'unavailable'}"
            )

        text = "".join(text_parts)
        self.router._record_success(profile)
        estimated_output_tokens = self.router.token_budget.estimate_tokens(text)
        self.router.token_budget.record_call(
            task_id=request.task_id,
            model=f"{profile}:{provider.model}",
            estimated_input_tokens=decision.estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            verified_required=request.verified_required,
            budget_overridden=decision.budget_overridden,
            reason=decision.reason,
        )
        actor = request.metadata.get("actor")
        if actor and not final_usage.get("actor"):
            final_usage["actor"] = str(actor)
        final_usage.setdefault(
            "estimated_input_tokens",
            decision.estimated_input_tokens,
        )
        final_usage.setdefault(
            "estimated_output_tokens",
            estimated_output_tokens,
        )
        if not final_usage.get("cost_usd") and not final_usage.get(
            "estimated_cost_usd"
        ):
            final_usage["estimated_cost_usd"] = self.router.pricing_table.estimate(
                provider=str(getattr(provider, "provider_name", "unknown")),
                model=str(provider.model),
                input_tokens=decision.estimated_input_tokens,
                output_tokens=estimated_output_tokens,
            )
        self.router.cost_ledger.record(
            task_id=request.task_id,
            task=request.task,
            profile=profile,
            model=str(provider.model),
            provider=str(getattr(provider, "provider_name", "unknown")),
            usage=final_usage,
            estimated_output_tokens=estimated_output_tokens,
        )
        if decision.cache_key and text:
            self.router.token_budget.put_cached(
                cache_key=decision.cache_key,
                model=f"{profile}:{provider.model}",
                response=text,
            )
        yield ModelDelta(
            sequence=final_sequence + 1,
            provider=str(getattr(provider, "provider_name", "unknown")),
            model=str(provider.model),
            profile=profile,
            usage=final_usage,
            finish_reason=final_reason,
            provider_request_id=provider_request_id,
            native=native,
        )


def streaming_router(router: ModelRouter) -> GovernedStreamingRouter:
    return GovernedStreamingRouter(router)
