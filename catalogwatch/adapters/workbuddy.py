from __future__ import annotations

from ..models import ModelRecord
from .openai_compat import OpenAIModelsAdapter


class WorkBuddyAdapter(OpenAIModelsAdapter):
    """Adapter for the local Buddy2api bridge.

    Buddy2api's `/v1/models` intentionally emits both `model-id` and
    `workbuddy/model-id` entries. Keep the bare ID when both exist; keep the
    prefixed form only when it is the sole entry (e.g. an international-only
    model exposed as `workbuddy-i/...`).
    """

    async def fetch_models(self) -> list[ModelRecord]:
        return self.dedupe(await super().fetch_models())

    @staticmethod
    def dedupe(records: list[ModelRecord]) -> list[ModelRecord]:
        prefixes = ("workbuddy/", "workbuddy-i/")
        bare_ids = {
            record.model_id
            for record in records
            if not record.model_id.startswith(prefixes)
        }
        result: list[ModelRecord] = []
        for record in records:
            for prefix in prefixes:
                if record.model_id.startswith(prefix):
                    if record.model_id[len(prefix) :] in bare_ids:
                        break
            else:
                result.append(record)
                continue
        return result
