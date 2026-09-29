# WebSocket: compressed message is corrupted when a ping/pong arrives between its fragments

## Problem

RFC 6455 §5.4 allows control frames (PING, PONG, CLOSE) to be interleaved between the
fragments of a fragmented data message. aiohttp's pure-Python WebSocket reader
(`aiohttp/_websocket/reader_py.py`, `WebSocketReader`) mishandles this when
permessage-deflate compression is negotiated.

If a compressed message is split into fragments and a control frame arrives in between,
the continuation fragment after the control frame is treated as the start of a new,
**uncompressed** message. As a result:

- for a BINARY message, the application receives raw deflate bytes instead of the
  original payload;
- for a TEXT message, the connection is closed with a UTF-8 decoding / protocol error.

## Reproduction

With a reader created with `compress=True`, feed:

1. a first fragment of a BINARY message: RSV1 set (compressed), FIN not set, carrying the first
   half of a raw-deflate payload (produced with `wbits=-9`, `Z_SYNC_FLUSH`, trailing
   `\x00\x00\xff\xff` stripped);
2. a PING frame with an empty payload (FIN set);
3. the final CONTINUATION fragment (FIN set, RSV1 clear) carrying the rest of the payload.

Expected output queue: a PING message followed by a BINARY message with the original,
decompressed data. Actual: the reassembled message is not decompressed.

## Expected behavior

- Control frames interleaved between fragments must not change the state of the fragmented
  data message they interrupt; the message is decompressed as a whole once it is complete.
- All currently valid WebSocket traffic keeps working.

## Acceptance

- Fix the bug in the pure-Python reader.
- Add tests.
- The WebSocket tests pass (pure-Python mode):
  `AIOHTTP_NO_EXTENSIONS=1 python -m pytest tests/test_websocket_parser.py tests/test_websocket_writer.py tests/test_web_websocket.py tests/test_web_websocket_functional.py tests/test_client_ws.py tests/test_client_ws_functional.py`
