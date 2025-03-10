# SPDX-License-Identifier: GPL-3.0
# Copyright (c) 2014-2023 William Edwards <shadowapex@gmail.com>, Benjamin Bean <superman2k5@gmail.com>
from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from typing import Optional, Union, final

from tuxemon import formula, monster
from tuxemon.event import get_npc
from tuxemon.event.eventaction import EventAction
from tuxemon.locale import T
from tuxemon.npc import NPC
from tuxemon.states.dialog import DialogState
from tuxemon.tools import open_dialog

logger = logging.getLogger(__name__)


# noinspection PyAttributeOutsideInit
@final
@dataclass
class SpawnMonsterAction(EventAction):
    """
    Breed a new monster.

    Add a new monster, created by breeding the two
    given mons (identified by instance_id, stored in a
    variable) and adds it to the given character's party
    (identified by slug). The parents must be in either
    the trainer's party, or a storage box owned by the
    trainer.

    Script usage:
        .. code-block::

            spawn_monster [npc_slug]

    Script parameters:
        npc_slug: Either "player" or npc slug name (e.g. "npc_maple").

    """

    name = "spawn_monster"
    npc_slug: Union[str, None] = None

    def start(self) -> None:
        trainer: Optional[NPC]

        if self.npc_slug is None:
            trainer = self.session.player
        else:
            trainer = get_npc(self.session, self.npc_slug)

        assert trainer, "No Trainer found with slug '{}'".format(
            self.npc_slug or "player"
        )

        mother_id = uuid.UUID(trainer.game_variables["breeding_mother"])
        father_id = uuid.UUID(trainer.game_variables["breeding_father"])

        mother = trainer.find_monster_by_id(mother_id)
        if mother is None:
            logger.debug("Mother not found in party, searching storage boxes.")
            mother = trainer.find_monster_in_storage(mother_id)

        father = trainer.find_monster_by_id(father_id)
        if father is None:
            logger.debug("Father not found in party, searching storage boxes.")
            father = trainer.find_monster_in_storage(father_id)

        if mother is None:
            logger.error(
                f"Could not find (mother) monster with instance id {mother_id}"
            )
            return
        if father is None:
            logger.error(
                f"Could not find (father) monster with instance id {father_id}"
            )
            return

        # matrix, it respects the types[0], strong against weak.
        # Mother (Water), Father (Earth)
        # Earth > Water => Child (Earth)
        if mother.types[0].water:
            if father.types[0].earth:
                seed = father
            else:
                seed = mother
        elif mother.types[0].fire:
            if father.types[0].water:
                seed = father
            else:
                seed = mother
        elif mother.types[0].wood:
            if father.types[0].metal:
                seed = father
            else:
                seed = mother
        elif mother.types[0].metal:
            if father.types[0].fire:
                seed = father
            else:
                seed = mother
        elif mother.types[0].earth:
            if father.types[0].wood:
                seed = father
            else:
                seed = mother
        else:
            seed = mother

        # retrieve the basic form
        for element in seed.history:
            if element.evo_stage.basic:
                seed_slug = element.mon_slug

        # continues the creation of the child.
        child = monster.Monster()
        child.load_from_db(seed_slug)
        child.set_level(5)
        child.set_moves(5)
        child.set_capture(formula.today_ordinal())
        child.current_hp = child.hp
        # child gets random father's moves
        father_moves = len(father.moves)
        replace_tech = random.randrange(0, 2)
        child.moves[replace_tech] = father.moves[
            random.randrange(0, father_moves - 1)
        ]
        trainer.add_monster(child, len(trainer.monsters))

        msg = T.format("got_new_tuxemon", {"monster_name": child.name})
        open_dialog(self.session, [msg])

    def update(self) -> None:
        try:
            self.session.client.get_state_by_name(DialogState)
        except ValueError:
            self.stop()
