import os
import tempfile


class Pipes:
    def __init__(self, input=None, output=None):
        self.input = input
        self.output = output

    def __iter__(self):
        if self.input is not None:
            yield self.input

        if self.output is not None:
            yield self.output

    async def close(self):
        for pipe in self:
            await pipe.close()


class Pipe:
    def __init__(self, middleware, buffered=False):
        self.middleware = middleware

        if buffered:
            self.w = tempfile.NamedTemporaryFile(buffering=0)
            self.r = open(self.w.name, "rb")
        else:
            r, w = os.pipe()
            self.r = os.fdopen(r, "rb")
            self.w = os.fdopen(w, "wb")

    async def close(self):
        await self.middleware.run_in_thread(self.r.close)
        await self.middleware.run_in_thread(self.w.close)
