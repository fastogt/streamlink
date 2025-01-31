import logging
import math
import re
from binascii import Error as BinasciiError, unhexlify
from datetime import datetime, timedelta
from typing import Callable, ClassVar, Dict, Iterator, List, Mapping, NamedTuple, Optional, Tuple, Type, Union
from urllib.parse import urljoin, urlparse

from isodate import ISO8601Error, parse_datetime  # type: ignore[import]
from requests import Response

from streamlink.logger import ALL, StreamlinkLogger


try:
    from typing import Any  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    from typing_extensions import Any


log: StreamlinkLogger = logging.getLogger(__name__)  # type: ignore[assignment]


class Resolution(NamedTuple):
    width: int
    height: int


# EXTINF
class ExtInf(NamedTuple):
    duration: float  # version >= 3: float
    title: Optional[str]


# EXT-X-BYTERANGE
class ByteRange(NamedTuple):  # version >= 4
    range: int
    offset: Optional[int]


# EXT-X-DATERANGE
class DateRange(NamedTuple):
    id: Optional[str]
    classname: Optional[str]
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    duration: Optional[timedelta]
    planned_duration: Optional[timedelta]
    end_on_next: bool
    x: Dict[str, str]


# EXT-X-KEY
class Key(NamedTuple):
    method: str
    uri: Optional[str]
    iv: Optional[bytes]  # version >= 2
    key_format: Optional[str]  # version >= 5
    key_format_versions: Optional[str]  # version >= 5


# EXT-X-MAP
class Map(NamedTuple):
    uri: str
    byterange: Optional[ByteRange]


# EXT-X-MEDIA
class Media(NamedTuple):
    uri: Optional[str]
    type: str
    group_id: str
    language: Optional[str]
    name: str
    default: bool
    autoselect: bool
    forced: bool
    characteristics: Optional[str]


# EXT-X-START
class Start(NamedTuple):
    time_offset: float
    precise: bool


# EXT-X-STREAM-INF
class StreamInfo(NamedTuple):
    bandwidth: int
    program_id: Optional[str]  # version < 6
    codecs: List[str]
    resolution: Optional[Resolution]
    audio: Optional[str]
    video: Optional[str]
    subtitles: Optional[str]


# EXT-X-I-FRAME-STREAM-INF
class IFrameStreamInfo(NamedTuple):
    bandwidth: int
    program_id: Optional[str]
    codecs: List[str]
    resolution: Optional[Resolution]
    video: Optional[str]


class Playlist(NamedTuple):
    uri: str
    stream_info: Union[StreamInfo, IFrameStreamInfo]
    media: List[Media]
    is_iframe: bool


class Segment(NamedTuple):
    uri: str
    duration: float
    title: Optional[str]
    key: Optional[Key]
    discontinuity: bool
    byterange: Optional[ByteRange]
    date: Optional[datetime]
    map: Optional[Map]


class M3U8:
    def __init__(self, uri: Optional[str] = None):
        self.uri = uri

        self.is_endlist: bool = False
        self.is_master: bool = False

        self.allow_cache: Optional[bool] = None  # version < 7
        self.discontinuity_sequence: Optional[int] = None
        self.iframes_only: Optional[bool] = None  # version >= 4
        self.media_sequence: Optional[int] = None
        self.playlist_type: Optional[str] = None
        self.targetduration: Optional[float] = None
        self.start: Optional[Start] = None
        self.version: Optional[int] = None

        self.media: List[Media] = []
        self.playlists: List[Playlist] = []
        self.dateranges: List[DateRange] = []
        self.segments: List[Segment] = []

    @classmethod
    def is_date_in_daterange(cls, date: Optional[datetime], daterange: DateRange):
        if date is None or daterange.start_date is None:
            return None

        if daterange.end_date is not None:
            return daterange.start_date <= date < daterange.end_date

        duration = daterange.duration or daterange.planned_duration
        if duration is not None:
            end = daterange.start_date + duration
            return daterange.start_date <= date < end

        return daterange.start_date <= date


_symbol_tag_parser = "__PARSE_TAG_NAME"


def parse_tag(tag: str):
    def decorator(func: Callable[[str], None]) -> Callable[[str], None]:
        setattr(func, _symbol_tag_parser, tag)

        return func

    return decorator


class M3U8ParserMeta(type):
    def __init__(cls, name, bases, namespace, **kwargs):
        super().__init__(name, bases, namespace, **kwargs)

        tags = dict(**getattr(cls, "_TAGS", {}))
        for member in namespace.values():
            tag = getattr(member, _symbol_tag_parser, None)
            if type(tag) is not str:  # noqa: E721
                continue
            tags[tag] = member
        cls._TAGS = tags


class M3U8Parser(metaclass=M3U8ParserMeta):
    _TAGS: ClassVar[Mapping[str, Callable[[Any, str], None]]]

    _extinf_re = re.compile(r"(?P<duration>\d+(\.\d+)?)(,(?P<title>.+))?")
    _attr_re = re.compile(r"""
        (?P<key>[A-Z0-9\-]+)
        =
        (?P<value>
            (?# decimal-integer)
            \d+
            (?# hexadecimal-sequence)
            |0[xX][0-9A-Fa-f]+
            (?# decimal-floating-point and signed-decimal-floating-point)
            |-?\d+\.\d+
            (?# quoted-string)
            |\"(?P<quoted>[^\r\n\"]*)\"
            (?# enumerated-string)
            |[^\",\s]+
            (?# decimal-resolution)
            |\d+x\d+
        )
        (?# be more lenient and allow spaces around attributes)
        \s*(?:,\s*|$)
    """, re.VERBOSE)
    _range_re = re.compile(r"(?P<range>\d+)(?:@(?P<offset>\d+))?")
    _tag_re = re.compile(r"#(?P<tag>[\w-]+)(:(?P<value>.+))?")
    _res_re = re.compile(r"(\d+)x(\d+)")

    def __init__(self, base_uri: Optional[str] = None, m3u8: Type[M3U8] = M3U8):
        self.m3u8: M3U8 = m3u8(base_uri)

        self._expect_playlist: bool = False
        self._streaminf: Optional[Dict[str, str]] = None

        self._expect_segment: bool = False
        self._extinf: Optional[ExtInf] = None
        self._byterange: Optional[ByteRange] = None
        self._discontinuity: bool = False
        self._map: Optional[Map] = None
        self._key: Optional[Key] = None
        self._date: Optional[datetime] = None

    @classmethod
    def create_stream_info(cls, streaminf: Mapping[str, Optional[str]], streaminfoclass=None):
        program_id = streaminf.get("PROGRAM-ID")

        _bandwidth = streaminf.get("BANDWIDTH")
        bandwidth = 0 if not _bandwidth else round(int(_bandwidth), 1 - int(math.log10(int(_bandwidth))))

        _resolution = streaminf.get("RESOLUTION")
        resolution = None if not _resolution else cls.parse_resolution(_resolution)

        codecs = (streaminf.get("CODECS") or "").split(",")

        if streaminfoclass is IFrameStreamInfo:
            return IFrameStreamInfo(
                bandwidth=bandwidth,
                program_id=program_id,
                codecs=codecs,
                resolution=resolution,
                video=streaminf.get("VIDEO"),
            )
        else:
            return StreamInfo(
                bandwidth=bandwidth,
                program_id=program_id,
                codecs=codecs,
                resolution=resolution,
                audio=streaminf.get("AUDIO"),
                video=streaminf.get("VIDEO"),
                subtitles=streaminf.get("SUBTITLES"),
            )

    @classmethod
    def split_tag(cls, line: str) -> Union[Tuple[str, str], Tuple[None, None]]:
        match = cls._tag_re.match(line)

        if match:
            return match.group("tag"), (match.group("value") or "").strip()

        return None, None

    @classmethod
    def parse_attributes(cls, value: str) -> Dict[str, str]:
        pos = 0
        length = len(value)
        res: Dict[str, str] = {}
        while pos < length:
            match = cls._attr_re.match(value, pos)
            if match is None:
                log.warning("Discarded invalid attributes list")
                res.clear()
                break
            pos = match.end()
            res[match["key"]] = match["quoted"] if match["quoted"] is not None else match["value"]

        return res

    @staticmethod
    def parse_bool(value: str) -> bool:
        return value == "YES"

    @classmethod
    def parse_byterange(cls, value: str) -> Optional[ByteRange]:
        match = cls._range_re.match(value)
        if match is None:
            return None

        _range, offset = match.groups()
        return ByteRange(
            range=int(_range),
            offset=int(offset) if offset is not None else None,
        )

    @classmethod
    def parse_extinf(cls, value: str) -> ExtInf:
        match = cls._extinf_re.match(value)
        if match is None:
            return ExtInf(0, None)

        return ExtInf(
            duration=float(match.group("duration")),
            title=match.group("title"),
        )

    @staticmethod
    def parse_hex(value: Optional[str]) -> Optional[bytes]:
        if value is None:
            return None

        if value[:2] in ("0x", "0X"):
            try:
                return unhexlify(f"{'0' * (len(value) % 2)}{value[2:]}")
            except BinasciiError:
                pass

        log.warning("Discarded invalid hexadecimal-sequence attribute value")
        return None

    @staticmethod
    def parse_iso8601(value: Optional[str]) -> Optional[datetime]:
        try:
            return None if value is None else parse_datetime(value)
        except (ISO8601Error, ValueError):
            log.warning("Discarded invalid ISO8601 attribute value")
            return None

    @staticmethod
    def parse_timedelta(value: Optional[str]) -> Optional[timedelta]:
        return None if value is None else timedelta(seconds=float(value))

    @classmethod
    def parse_resolution(cls, value: str) -> Resolution:
        match = cls._res_re.match(value)
        if match is None:
            return Resolution(width=0, height=0)

        return Resolution(
            width=int(match.group(1)),
            height=int(match.group(2)),
        )

    # ----

    # 4.3.1: Basic Tags

    @parse_tag("EXT-X-VERSION")
    def parse_tag_ext_x_version(self, value: str) -> None:
        """
        EXT-X-VERSION
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.1.2
        """
        self.m3u8.version = int(value)

    # 4.3.2: Media Segment Tags

    @parse_tag("EXTINF")
    def parse_tag_extinf(self, value: str) -> None:
        """
        EXTINF
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.1
        """
        self._expect_segment = True
        self._extinf = self.parse_extinf(value)

    @parse_tag("EXT-X-BYTERANGE")
    def parse_tag_ext_x_byterange(self, value: str) -> None:
        """
        EXT-X-BYTERANGE
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.2
        """
        self._expect_segment = True
        self._byterange = self.parse_byterange(value)

    # noinspection PyUnusedLocal
    @parse_tag("EXT-X-DISCONTINUITY")
    def parse_tag_ext_x_discontinuity(self, value: str) -> None:
        """
        EXT-X-DISCONTINUITY
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.3
        """
        self._discontinuity = True
        self._map = None

    @parse_tag("EXT-X-KEY")
    def parse_tag_ext_x_key(self, value: str) -> None:
        """
        EXT-X-KEY
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.4
        """
        attr = self.parse_attributes(value)
        method = attr.get("METHOD")
        uri = attr.get("URI")

        if not method:
            return

        self._key = Key(
            method=method,
            uri=self.uri(uri) if uri else None,
            iv=self.parse_hex(attr.get("IV")),
            key_format=attr.get("KEYFORMAT"),
            key_format_versions=attr.get("KEYFORMATVERSIONS"),
        )

    @parse_tag("EXT-X-MAP")
    def parse_tag_ext_x_map(self, value: str) -> None:  # version >= 5
        """
        EXT-X-MAP
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.5
        """
        attr = self.parse_attributes(value)
        uri = attr.get("URI")

        if not uri:
            return

        byterange = self.parse_byterange(attr.get("BYTERANGE", ""))
        self._map = Map(
            uri=self.uri(uri),
            byterange=byterange,
        )

    @parse_tag("EXT-X-PROGRAM-DATE-TIME")
    def parse_tag_ext_x_program_date_time(self, value: str) -> None:
        """
        EXT-X-PROGRAM-DATE-TIME
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.6
        """
        self._date = self.parse_iso8601(value)

    @parse_tag("EXT-X-DATERANGE")
    def parse_tag_ext_x_daterange(self, value: str) -> None:
        """
        EXT-X-DATERANGE
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.7
        """
        attr = self.parse_attributes(value)
        daterange = DateRange(
            id=attr.pop("ID", None),
            classname=attr.pop("CLASS", None),
            start_date=self.parse_iso8601(attr.pop("START-DATE", None)),
            end_date=self.parse_iso8601(attr.pop("END-DATE", None)),
            duration=self.parse_timedelta(attr.pop("DURATION", None)),
            planned_duration=self.parse_timedelta(attr.pop("PLANNED-DURATION", None)),
            end_on_next=self.parse_bool(attr.pop("END-ON-NEXT", "NO")),
            x=attr,
        )
        self.m3u8.dateranges.append(daterange)

    # 4.3.3: Media Playlist Tags

    @parse_tag("EXT-X-TARGETDURATION")
    def parse_tag_ext_x_targetduration(self, value: str) -> None:
        """
        EXT-X-TARGETDURATION
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.3.1
        """
        self.m3u8.targetduration = float(value)

    @parse_tag("EXT-X-MEDIA-SEQUENCE")
    def parse_tag_ext_x_media_sequence(self, value: str) -> None:
        """
        EXT-X-MEDIA-SEQUENCE
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.3.2
        """
        self.m3u8.media_sequence = int(value)

    @parse_tag("EXT-X-DISCONTINUTY-SEQUENCE")
    def parse_tag_ext_x_discontinuity_sequence(self, value: str) -> None:
        """
        EXT-X-DISCONTINUITY-SEQUENCE
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.3.3
        """
        self.m3u8.discontinuity_sequence = int(value)

    # noinspection PyUnusedLocal
    @parse_tag("EXT-X-ENDLIST")
    def parse_tag_ext_x_endlist(self, value: str) -> None:
        """
        EXT-X-ENDLIST
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.3.4
        """
        self.m3u8.is_endlist = True

    @parse_tag("EXT-X-PLAYLIST-TYPE")
    def parse_tag_ext_x_playlist_type(self, value: str) -> None:
        """
        EXT-X-PLAYLISTTYPE
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.3.5
        """
        self.m3u8.playlist_type = value

    # noinspection PyUnusedLocal
    @parse_tag("EXT-X-I-FRAMES-ONLY")
    def parse_tag_ext_x_i_frames_only(self, value: str) -> None:  # version >= 4
        """
        EXT-X-I-FRAMES-ONLY
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.3.6
        """
        self.m3u8.iframes_only = True

    # 4.3.4: Master Playlist Tags

    @parse_tag("EXT-X-MEDIA")
    def parse_tag_ext_x_media(self, value: str) -> None:
        """
        EXT-X-MEDIA
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.4.1
        """
        attr = self.parse_attributes(value)
        _type = attr.get("TYPE")
        uri = attr.get("URI")
        group_id = attr.get("GROUP-ID")
        name = attr.get("NAME")

        if not _type or not group_id or not name:
            return

        media = Media(
            type=_type,
            uri=self.uri(uri) if uri else None,
            group_id=group_id,
            language=attr.get("LANGUAGE"),
            name=name,
            default=self.parse_bool(attr.get("DEFAULT", "NO")),
            autoselect=self.parse_bool(attr.get("AUTOSELECT", "NO")),
            forced=self.parse_bool(attr.get("FORCED", "NO")),
            characteristics=attr.get("CHARACTERISTICS"),
        )
        self.m3u8.media.append(media)

    @parse_tag("EXT-X-STREAM-INF")
    def parse_tag_ext_x_stream_inf(self, value: str) -> None:
        """
        EXT-X-STREAM-INF
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.4.2
        """
        self._expect_playlist = True
        self._streaminf = self.parse_attributes(value)

    @parse_tag("EXT-X-I-FRAME-STREAM-INF")
    def parse_tag_ext_x_i_frame_stream_inf(self, value: str) -> None:
        """
        EXT-X-I-FRAME-STREAM-INF
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.4.3
        """
        attr = self.parse_attributes(value)
        uri = attr.get("URI")

        streaminf = self._streaminf or attr
        self._streaminf = None

        if not uri:
            return

        stream_info = self.create_stream_info(streaminf, IFrameStreamInfo)
        playlist = Playlist(
            uri=self.uri(uri),
            stream_info=stream_info,
            media=[],
            is_iframe=True,
        )
        self.m3u8.playlists.append(playlist)

    @parse_tag("EXT-X-SESSION-DATA")
    def parse_tag_ext_x_session_data(self, value: str) -> None:
        """
        EXT-X-SESSION-DATA
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.4.4
        """

    @parse_tag("EXT-X-SESSION-KEY")
    def parse_tag_ext_x_session_key(self, value: str) -> None:
        """
        EXT-X-SESSION-KEY
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.4.5
        """

    # 4.3.5: Media or Master Playlist Tags

    @parse_tag("EXT-X-INDEPENDENT-SEGMENTS")
    def parse_tag_ext_x_independent_segments(self, value: str) -> None:
        """
        EXT-X-INDEPENDENT-SEGMENTS
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.5.1
        """

    @parse_tag("EXT-X-START")
    def parse_tag_ext_x_start(self, value: str) -> None:
        """
        EXT-X-START
        https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.5.2
        """
        attr = self.parse_attributes(value)
        self.m3u8.start = Start(
            time_offset=float(attr.get("TIME-OFFSET", 0)),
            precise=self.parse_bool(attr.get("PRECISE", "NO")),
        )

    # Removed tags
    # https://datatracker.ietf.org/doc/html/rfc8216#section-7

    @parse_tag("EXT-X-ALLOW-CACHE")
    def parse_tag_ext_x_allow_cache(self, value: str) -> None:  # version < 7
        self.m3u8.allow_cache = self.parse_bool(value)

    # ----

    def parse_line(self, line: str) -> None:
        if line.startswith("#"):
            tag, value = self.split_tag(line)
            if not tag or value is None or tag not in self._TAGS:
                return
            self._TAGS[tag](self, value)

        elif self._expect_segment:
            self._expect_segment = False
            segment = self.get_segment(self.uri(line))
            self.m3u8.segments.append(segment)

        elif self._expect_playlist:
            self._expect_playlist = False
            playlist = self.get_playlist(self.uri(line))
            self.m3u8.playlists.append(playlist)

    def parse(self, data: Union[str, Response]) -> M3U8:
        lines: Iterator[str]
        if isinstance(data, str):
            lines = iter(filter(bool, data.splitlines()))
        else:
            lines = iter(filter(bool, data.iter_lines(decode_unicode=True)))

        try:
            line = next(lines)
        except StopIteration:
            return self.m3u8
        else:
            if not line.startswith("#EXTM3U"):
                log.warning(f"Malformed HLS Playlist. Expected #EXTM3U, but got {line[:250]}")
                raise ValueError("Missing #EXTM3U header")

        lines = log.iter(ALL, lines)

        parse_line = self.parse_line
        for line in lines:
            parse_line(line)

        # Associate Media entries with each Playlist
        for playlist in self.m3u8.playlists:
            for media_type in ("audio", "video", "subtitles"):
                group_id = getattr(playlist.stream_info, media_type, None)
                if group_id:
                    for media in filter(lambda m: m.group_id == group_id, self.m3u8.media):
                        playlist.media.append(media)

        self.m3u8.is_master = not not self.m3u8.playlists

        return self.m3u8

    def uri(self, uri: str) -> str:
        if uri and urlparse(uri).scheme:
            return uri
        elif uri and self.m3u8.uri:
            return urljoin(self.m3u8.uri, uri)
        else:
            return uri

    def get_segment(self, uri: str) -> Segment:
        extinf: ExtInf = self._extinf or ExtInf(0, None)
        self._extinf = None

        discontinuity = self._discontinuity
        self._discontinuity = False

        byterange = self._byterange
        self._byterange = None

        date = self._date
        self._date = None

        return Segment(
            uri=uri,
            duration=extinf.duration,
            title=extinf.title,
            key=self._key,
            discontinuity=discontinuity,
            byterange=byterange,
            date=date,
            map=self._map,
        )

    def get_playlist(self, uri: str) -> Playlist:
        streaminf = self._streaminf or {}
        self._streaminf = None

        stream_info = self.create_stream_info(streaminf)

        return Playlist(
            uri=uri,
            stream_info=stream_info,
            media=[],
            is_iframe=False,
        )


def load(
    data: Union[str, Response],
    base_uri: Optional[str] = None,
    parser: Type[M3U8Parser] = M3U8Parser,
    **kwargs,
) -> M3U8:
    """
    Parse an M3U8 playlist from a string of data or an HTTP response.

    If specified, *base_uri* is the base URI that relative URIs will
    be joined together with, otherwise relative URIs will be as is.

    If specified, *parser* can be an M3U8Parser subclass to be used
    to parse the data.
    """
    if base_uri is None and isinstance(data, Response):
        base_uri = data.url

    return parser(base_uri, **kwargs).parse(data)
