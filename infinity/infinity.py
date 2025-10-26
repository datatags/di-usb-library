from collections import defaultdict
import asyncio
import hid

DEVICE_VID = 0x0e6f
DEVICE_PID = 0x0129

class Tag:
    def __init__(self, index: bytes):
        self.platform = index[0] >> 4
        self.index    = index[0] & 0x0F
        # ISO 14443A SAK, always 0x09 for DI tags
        self.sak      = index[1]
        self.uid      = None

    def __str__(self):
        return f"Tag(platform={self.platform},index={self.index},sak={self.sak},uid={self.uid})"

    def __repr__(self):
        return str(self)


class TagChangeEvent:
    def __init__(self, data: bytes):
        self.platform = data[2]
        self.tag_type = data[3]
        self.tag_index = data[4]
        self.is_removed = bool(data[5])


class InfinityComms:
    def __init__(self):
        self.device = self._initBase()
        self.finish = False
        self.pending_requests = {}
        self.message_number = 0
        self.observers = []
        self.lock = asyncio.Lock()

    def _initBase(self):
        device = hid.Device(DEVICE_VID, DEVICE_PID)
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
                asyncio.create_task(self._notifyObservers(TagChangeEvent(fields)))
                continue
            self._unknown_message(fields)

    def addObserver(self, object):
        self.observers.append(object)

    async def _notifyObservers(self, event: TagChangeEvent):
        for obs in self.observers:
            await obs.tagsUpdated(event)

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
    def __init__(self):
        self.comms = InfinityComms()
        self.comms.addObserver(self)
        self.onTagsChanged = None

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

    async def tagsUpdated(self, event: TagChangeEvent):
        if self.onTagsChanged:
            await self.onTagsChanged(event)

    async def getAllTags(self) -> dict[int, list[Tag]]:
        tags = await self.getTagIndex()
        if len(tags) == 0:
            return {}
        tagByPlatform = defaultdict(list)
        for tag in tags:
            await self.loadTagUid(tag)
            tagByPlatform[tag.platform].append(tag)
        return dict(tagByPlatform)

    async def getTagIndex(self) -> list[Tag]:
        data = await self.comms.send_message(0xa1)
        tags = []
        for i in range(0, len(data), 2):
            tags.append(Tag(data[i:i+2]))
        return tags

    async def loadTagUid(self, tag: Tag):
        # First byte is a status or something, 0x00 if the tag exists, 0x80 if it doesn't
        tag.uid = (await self.comms.send_message(0xb4, [tag.index]))[1:]

    async def setColor(self, platform: int, r: int, g: int, b: int):
        await self.comms.send_message(0x90, [platform, r, g, b])

    async def fadeColor(self, platform: int, r: int, g: int, b: int):
        await self.comms.send_message(0x92, [platform, 0x10, 0x02, r, g, b])

    async def flashColor(self, platform: int, r: int, g: int, b: int):
        await self.comms.send_message(0x93, [platform, 0x02, 0x02, 0x06, r, g, b])

async def main():
    base = InfinityBase()

    async def onChange(event: TagChangeEvent):
        tags = await base.getAllTags()
        color = (0, 0, 0)
        count = len(tags.get(event.platform, []))
        if count == 1:
            color = (0, 0, 200)
        elif count == 2:
            color = (0, 56, 0)
        elif count > 2:
            color = (200, 0, 0)
        await base.setColor(event.platform, *color)

    base.onTagsChanged = onChange

    await base.connect()

    print(f"Tags: {await base.getAllTags()}")

    await base.setColor(1, 200, 0, 0)

    await base.setColor(2, 0, 56, 0)

    await base.fadeColor(3, 0, 0, 200)

    await asyncio.sleep(3)

    await base.flashColor(3, 0, 0, 200)

    print("Try adding and removing figures and discs to/from the base. Ctrl-C to quit")
    await base.comms_task

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
