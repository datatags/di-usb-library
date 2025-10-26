from collections import defaultdict
import asyncio
import hid

DEVICE_VID = 0x0e6f
DEVICE_PID = 0x0129

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
                self._notifyObservers()
                continue
            self._unknown_message(fields)

    def addObserver(self, object):
        self.observers.append(object)

    def _notifyObservers(self):
        for obs in self.observers:
            obs.tagsUpdated()

    def _unknown_message(self, fields):
        print("UNKNOWN MESSAGE RECEIVED ", fields)

    def _next_message_number(self):
        self.message_number = (self.message_number + 1) % 256
        return self.message_number

    async def send_message(self, command, data = []):
        message_id, message = self._construct_message(command, data)
        result = asyncio.get_event_loop().create_future()
        self.pending_requests[message_id] = result
        async with self.lock:
            self.device.write(bytes(message))
        return await result

    def _construct_message(self, command, data):
        message_id = self._next_message_number()
        command_body = [command, message_id] + data
        command_length = len(command_body)
        command_bytes = [0x00, 0xff, command_length] + command_body
        message = [0x00] * 33
        checksum = 0
        for (index, byte) in enumerate(command_bytes):
            message[index] = byte
            checksum = checksum + byte
        message[len(command_bytes)] = checksum & 0xff
        return (message_id, message)


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

    def tagsUpdated(self):
        if self.onTagsChanged:
            self.onTagsChanged()

    async def getAllTags(self) -> dict[int, list[list[int]]]:
        idx = await self.getTagIdx()
        if len(idx) == 0:
            return {}
        tagByPlatform = defaultdict(list)
        for (platform, tagIdx) in idx:
            tag = await self.getTag(tagIdx)
            tagByPlatform[platform].append(tag)
        return dict(tagByPlatform)

    async def getTagIdx(self) -> list[tuple[int, int]]:
        data = await self.comms.send_message(0xa1)
        return [ ((byte & 0xF0) >> 4, byte & 0x0F) for byte in data if byte != 0x09]

    async def getTag(self, idx) -> list[int]:
        return await self.comms.send_message(0xb4, [idx])

    async def setColor(self, platform: int, r: int, g: int, b: int):
        await self.comms.send_message(0x90, [platform, r, g, b])

    async def fadeColor(self, platform: int, r: int, g: int, b: int):
        await self.comms.send_message(0x92, [platform, 0x10, 0x02, r, g, b])

    async def flashColor(self, platform: int, r: int, g: int, b: int):
        await self.comms.send_message(0x93, [platform, 0x02, 0x02, 0x06, r, g, b])

async def main():
    def futurePrint(s):
        print(s)

    base = InfinityBase()

    base.onTagsChanged = lambda: futurePrint("Tags added or removed.")

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
