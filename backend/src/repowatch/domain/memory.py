from typing import Annotated

from pydantic import Field

from repowatch.domain.protocols import Contract

Text = Annotated[str, Field(min_length=1, max_length=16000, strict=True)]


class MemoryContent(Contract):
    symptoms: Text
    applicability: Text
    method: Text
    verification: Text
    limitations: Text
