from pydantic import BaseModel, ConfigDict

type Point2 = tuple[float, float]


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        str_strip_whitespace=True,
        populate_by_name=True,
        extra="forbid",
    )
