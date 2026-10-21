from typing import Annotated

from pydantic import Field

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
EvidenceId = Annotated[str, Field(pattern=r"^E\d+$")]
