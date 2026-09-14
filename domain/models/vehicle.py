from pydantic import BaseModel


class Vehicle(BaseModel):
    registration: str
    make: str
    model: str
    year: int | None = None
