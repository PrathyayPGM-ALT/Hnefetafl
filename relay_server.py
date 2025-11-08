# relay_server.py
import asyncio, json, random

# room_code -> {"a": {"r": reader, "w": writer, "name": str},
#               "b": {"r": reader, "w": writer, "name": str}}
rooms = {}

async def send(writer, obj):
    data = (json.dumps(obj) + "\n").encode("utf-8")
    writer.write(data)
    await writer.drain()

def players_list(roommap):
    out = []
    for k in ("a","b"):
        if k in roommap:
            out.append(roommap[k]["name"])
    return out

async def notify_room(room, obj, exclude=None):
    for k in ("a","b"):
        if k in room and room[k]["w"] is not exclude:
            try:
                await send(room[k]["w"], obj)
            except Exception:
                pass

async def handle_client(reader, writer):
    slot = None
    room_code = None
    try:
        # Expect: {"type":"join","room":"1234","name":"Mr X"}
        line = await reader.readline()
        if not line:
            writer.close(); await writer.wait_closed(); return
        hello = json.loads(line.decode("utf-8").strip())
        if hello.get("type") != "join" or "room" not in hello:
            await send(writer, {"type":"error","msg":"bad join"})
            writer.close(); await writer.wait_closed(); return

        room_code = str(hello["room"])
        name = str(hello.get("name","Player"))

        rooms.setdefault(room_code, {})

        if "a" not in rooms[room_code]:
            slot = "a"
            rooms[room_code]["a"] = {"r": reader, "w": writer, "name": name}
        elif "b" not in rooms[room_code]:
            slot = "b"
            rooms[room_code]["b"] = {"r": reader, "w": writer, "name": name}
        else:
            await send(writer, {"type":"full"})
            writer.close(); await writer.wait_closed(); return

        # Tell everyone current waiting roster
        await notify_room(rooms[room_code], {"type":"waiting","players": players_list(rooms[room_code])})
        await notify_room(rooms[room_code], {"type":"joined","name": name})

        # If pair complete, randomize sides & start
        rm = rooms[room_code]
        if "a" in rm and "b" in rm:
            sides = ["DEFENDER","ATTACKER"]
            random.shuffle(sides)
            current = "ATTACKER"   # or "DEFENDER" if you prefer

            a, b = rm["a"], rm["b"]
            await send(a["w"], {"type":"start","your_side":sides[0],"current_player":current,
                                "opponent_name": b["name"]})
            await send(b["w"], {"type":"start","your_side":sides[1],"current_player":current,
                                "opponent_name": a["name"]})

        # Forward loop
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8").strip())
            except Exception:
                continue
            # Forward to other peer
            rm = rooms.get(room_code, {})
            other = "b" if slot == "a" else "a"
            if other in rm:
                await send(rm[other]["w"], obj)

    except Exception:
        pass
    finally:
        # Clean-up
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass
        if room_code in rooms:
            rm = rooms[room_code]
            leaving_name = None
            for k in ("a","b"):
                if k in rm and rm[k]["w"] is writer:
                    leaving_name = rm[k]["name"]
                    del rm[k]
                    break
            if leaving_name:
                # Notify remaining player that opponent left (stay waiting)
                await notify_room(rm, {"type":"opponent_left","name": leaving_name})
                await notify_room(rm, {"type":"waiting","players": players_list(rm)})
            if not rm:
                del rooms[room_code]

async def main():
    server = await asyncio.start_server(handle_client, host="0.0.0.0", port=8765)
    print("Relay listening on 0.0.0.0:8765")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())

