from dataclasses import dataclass, field


@dataclass
class Segment:
    type: str       # 'text' | 'image'
    value: str = ''
    ref: str = ''   # asset key for image segments
    confidence: float = 1.0


@dataclass
class Block:
    index: int
    segments: list = field(default_factory=list)
    style: dict = field(default_factory=dict)
    list_label: str | None = None
    page: int = 0
    bbox: tuple | None = None  # (x0, top, x1, bottom) in page coordinates, if known

    @property
    def text(self):
        return ''.join(s.value for s in self.segments if s.type == 'text')
