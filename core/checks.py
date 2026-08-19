from __future__ import annotations

import disnake


def is_staff(member: disnake.Member, *role_ids: int) -> bool:
    """Считается персоналом, если есть право «Управление сервером» либо одна из указанных ролей."""
    if member.guild_permissions.manage_guild:
        return True
    member_role_ids = {role.id for role in member.roles}
    return any(rid and rid in member_role_ids for rid in role_ids)
