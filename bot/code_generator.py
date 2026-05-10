import secrets


ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 4


async def generate_unique_room_code(repo) -> str:
    """Generate a unique room code.

    Tries up to 20 random codes; if all collide with previously used codes,
    falls back to recycling the oldest used code.
    """
    for _ in range(20):
        code = "".join(
            secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH)
        )
        if not await repo.code_ever_used(code):
            return code
    return await repo.oldest_used_code()
