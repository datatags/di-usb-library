from dataclasses import dataclass, astuple
from collections import defaultdict
from enum import Enum
import asyncio
import hid

DEVICE_VID = 0x0e6f
DEVICE_PID = 0x0129


@dataclass
class Color:
    """Simple class to avoid having to pass around r/g/b separately

    Each value should 0-255, inclusive.
    """
    r: int
    g: int
    b: int

    def __iter__(self):
        # https://stackoverflow.com/a/59569566 for easier unpacking
        return iter(astuple(self))


class Platform(Enum):
    ALL_PLATFORMS = 0
    HEXAGONAL = 1
    PLAYER_ONE = 2
    PLAYER_TWO = 3

    def __int__(self):
        return self.value


class ErrorType(Enum):
    SUCCESS = (0x00, "Success")
    NO_SUCH_TAG = (0x80, "No such tag") # Occurs when requesting info on a tag index that isn't there
    TAG_IO_ERROR = (0x82, "Tag I/O error") # General failure, e.g. auth failure or tag removed during communication
    TAG_AUTH_UNSUPPORTED = (0x83, "Tag auth unsupported") # Tag requires an unsupported authentication method

    def __new__(cls, val: int, msg: str):
        entry = object.__new__(cls)
        entry._value_ = val
        entry.msg = msg
        return entry

    def __str__(self):
        return self.msg


class Tag:
    def __init__(self, platform: int | Platform, index: int, sak: int):
        self.platform = platform
        self.index    = index
        # ISO 14443A SAK, always 0x09 for DI tags (Mifare Classic Mini)
        self.sak      = sak
        self.uid      = None

    @staticmethod
    def from_bytes(index: bytes):
        return Tag(index[0] >> 4, index[0] & 0x0F, index[1])

    def __str__(self):
        return f"Tag(platform={int(self.platform)},index={self.index},sak={self.sak},uid={self.uid})"

    def __repr__(self):
        return str(self)


class TagChangeEvent:
    def __init__(self, data: bytes):
        self.tag = Tag(data[2], data[4], data[3])
        self.is_removed = bool(data[5])


class InfinityComms:
    def __init__(self, serial: str | None = None):
        self.device = self._init_base(serial)
        self.finish = False
        self.pending_requests = {}
        self.message_number = 0
        self.observers = []
        self.lock = asyncio.Lock()

    def _init_base(self, serial: str | None):
        device = hid.Device(DEVICE_VID, DEVICE_PID, serial)
        print(f"Connected to {device.serial}")
        device.nonblocking = False
        return device

    async def run(self):
        while not self.finish:
            fields = await asyncio.get_event_loop().run_in_executor(None, self.device.read, 32, 1000)
            if len(fields) == 0:
                continue

            if fields[0] == 0xaa:
                length = fields[1]
                message_id = fields[2]
                if message_id in self.pending_requests:
                    self.pending_requests[message_id].set_result(fields[3:length+2])
                    del self.pending_requests[message_id]
                    continue
            elif fields[0] == 0xab:
                # Do on a separate task in case observers send commands
                asyncio.create_task(self._notify_observers(TagChangeEvent(fields)))
                continue
            self._unknown_message(fields)

    def add_observer(self, object):
        self.observers.append(object)

    async def _notify_observers(self, event: TagChangeEvent):
        for obs in self.observers:
            await obs.tags_updated(event)

    def _unknown_message(self, fields):
        print("UNKNOWN MESSAGE RECEIVED ", fields)

    def _next_message_number(self):
        self.message_number = (self.message_number + 1) % 256
        return self.message_number

    async def send_message(self, command: int, data: list[int] = []):
        message_id, message = self._construct_message(command, bytes(data))
        result = asyncio.get_event_loop().create_future()
        self.pending_requests[message_id] = result
        async with self.lock:
            self.device.write(message)
        return await result

    def _construct_message(self, command: int, data: bytes):
        message_id = self._next_message_number()
        command_bytes = b"\x00\xff"
        def to_bytes(val: int):
            return val.to_bytes(1, byteorder="big")
        command_bytes += to_bytes(2 + len(data))
        command_bytes += to_bytes(command)
        command_bytes += to_bytes(message_id)
        command_bytes += data

        checksum = 0
        for byte in command_bytes:
            checksum += byte
        command_bytes += to_bytes(checksum & 0xFF)
        # Previous implementation padded out the message to 33 bytes with zeroes,
        # but this doesn't seem to be necessary
        return (message_id, command_bytes)


class InfinityBase(object):
    """
    Another Disney Infinity base implementation was referenced for some of the advanced
    lighting control features here, see:
    https://github.com/johndrinkwater/Disney-Infinity-and-Skylanders-Lighting
    """
    def __init__(self, serial: str | None = None):
        self.comms = InfinityComms(serial)
        self.comms.add_observer(self)
        self.on_tags_changed = None

    async def connect(self):
        self.comms_task = asyncio.get_event_loop().create_task(self.comms.run())
        await self.activate()

    def disconnect(self):
        self.comms.finish = True
        self.comms_task.cancel()

    async def activate(self):
        activate_message = [0x28,0x63,0x29,0x20,0x44,
                            0x69,0x73,0x6e,0x65,0x79,
                            0x20,0x32,0x30,0x31,0x33]
        await self.comms.send_message(0x80, activate_message)

    async def tags_updated(self, event: TagChangeEvent):
        if self.on_tags_changed:
            await self.on_tags_changed(event)

    async def get_all_tags(self) -> dict[int, list[Tag]]:
        tags = await self.get_tag_index()
        if len(tags) == 0:
            return {}
        tagByPlatform = defaultdict(list)
        for tag in tags:
            try:
                await self.load_tag_uid(tag)
            except ValueError as e:
                print(e)
            tagByPlatform[tag.platform].append(tag)
        return dict(tagByPlatform)

    async def get_tag_index(self) -> list[Tag]:
        data = await self.comms.send_message(0xa1)
        tags = []
        for i in range(0, len(data), 2):
            tags.append(Tag.from_bytes(data[i:i+2]))
        return tags

    async def load_tag_uid(self, tag: Tag):
        data = await self.comms.send_message(0xb4, [tag.index])
        # First byte is a status or something, 0x00 if the tag exists, 0x80 if it doesn't
        if data[0] == ErrorType.NO_SUCH_TAG.value:
            raise ValueError("No such tag")
        tag.uid = data[1:]

    async def set_color(self, platform: int | Platform, color: Color):
        """Set the color of a platform

        Arguments:
        platform -- the platform to control
        color -- the color to set the platform to
        """
        await self.comms.send_message(0x90, [int(platform), *color])

    async def fade_color(self, platform: int | Platform, color: Color, duration: int = 0x10, count: int = 2):
        """Fade a platform color in and out according to the parameters.

        Arguments:
        platform -- the platform to control
        color -- the color to make the platform
        duration -- the duration of the cycle in 1/16-second increments. 16 = 0x10 = 1 second
        count -- the number of half-cycles to perform, e.g. 1 is off-to-on, 2 is off-on-off, etc
        """
        await self.comms.send_message(0x92, [int(platform), duration, count, *color])

    async def flash_color(self, platform: int | Platform, color: Color, onTime: int = 0x02, offTime: int = 0x02, count: int = 0x06):
        """Flash a platform on and off

        Arguments:
        platform -- the platform to control
        color -- the color to make the platform
        onTime -- the duration of each on-cycle in 1/16-second increments. 16 = 0x10 = 1 second
        offTime -- the duration of each off-cycle in 1/16 second increments. 16 = 0x10 = 1 second
        count -- the number of half-cycles to perform, e.g. 1 is off-to-on, 2 is off-on-off, etc
        """
        await self.comms.send_message(0x93, [int(platform), onTime, offTime, count, *color])

    async def fade_random(self, platform: int | Platform, duration: int = 0x10, count: int = 0x02):
        """Fade a platform between its current color and random other colors

        Arguments:
        platform -- the platform to control
        onTime -- the duration of each on-cycle in 1/16-second increments. 16 = 0x10 = 1 second
        count -- the number of half-cycles to perform, e.g. 1 is src-to-dest, 2 is src-dest-src, etc
        """
        await self.comms.send_message(0x94, [int(platform), duration, count])


    def _check_for_error(self, code: int):
        if code == ErrorType.SUCCESS.value:
            return # yay!
        try:
            error = ErrorType(code)
        except ValueError:
            raise ValueError(f"Unknown error: {hex(code)}")
        raise ValueError(error.msg)

    async def read_tag(self, tag: Tag, sector: int, offset: int = 0) -> bytes:
        """Read a data block from the tag.

        The actual block read is `(sector * 4) + offset`, and there don't
        appear to be any artificial limits on the parameters, e.g. using
        sector=0 offset=14 works just as well as sector=3 offset=2

        Keyword arguments:
        tag -- the tag to read from
        sector -- the sector to read from
        offset -- the offset within the sector to read
        """
        data = await self.comms.send_message(0xa2, [tag.index, sector, offset])
        self._check_for_error(data[0])
        return data[1:]

    async def write_tag(self, tag: Tag, sector: int, data: bytes, offset: int = 0):
        """Write a data block to the tag. See `read_tag` for more info."""
        if len(data) != 16:
            raise ValueError("Must supply exactly 16 bytes")
        data = await self.comms.send_message(0xa3, [tag.index, sector, offset] + list(data))
        self._check_for_error(data[0])

async def run_base(serial: str):
    base = InfinityBase(serial)
    off = Color(0, 0, 0)
    red = Color(200, 0, 0)
    green = Color(0, 56, 0)
    blue = Color(0, 0, 200)

    async def on_change(event: TagChangeEvent):
        if not event.is_removed:
            try:
                data = await base.read_tag(event.tag, 0)
                print(f"Tag data, block 0: {data.hex()}")
            except ValueError as e:
                print(f"Failed to read tag data: {e}")

        tags = await base.get_all_tags()
        color = off
        count = len(tags.get(event.tag.platform, []))
        if count == 1:
            color = blue
        elif count == 2:
            color = green
        elif count > 2:
            color = red
        await base.set_color(event.tag.platform, color)

    base.on_tags_changed = on_change

    await base.connect()

    print(f"Tags: {await base.get_all_tags()}")

    await base.set_color(1, red)

    await base.set_color(2, green)

    await base.fade_color(3, blue)

    await asyncio.sleep(3)

    await base.fade_random(2, 0x10, 0x11)
    await base.flash_color(3, blue, 0x8, 0x8, 9)

    await base.comms_task # sleep forever

async def main():
    tasks = []
    for dev in hid.enumerate(DEVICE_VID, DEVICE_PID):
        tasks.append(asyncio.create_task(run_base(dev["serial_number"])))
    if len(tasks) == 0:
        print("No bases detected!")
        return
    await asyncio.sleep(3)
    print("Try adding and removing figures and discs to/from the base. Ctrl-C to quit")
    await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
