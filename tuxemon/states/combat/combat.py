# SPDX-License-Identifier: GPL-3.0
# Copyright (c) 2014-2023 William Edwards <shadowapex@gmail.com>, Benjamin Bean <superman2k5@gmail.com>
from __future__ import annotations

import datetime as dt
import logging
import random
from collections import defaultdict
from functools import partial
from itertools import chain
from typing import (
    Dict,
    Iterable,
    List,
    Literal,
    MutableMapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import pygame
from pygame.rect import Rect

from tuxemon import audio, graphics, state, tools
from tuxemon.animation import Task
from tuxemon.battle import Battle
from tuxemon.combat import (
    check_moves,
    confused,
    defeated,
    fainted,
    generic,
    get_awake_monsters,
    has_effect,
    has_effect_param,
    has_status,
    has_status_bond,
    pre_checking,
    scope,
    spyderbite,
)
from tuxemon.db import (
    BattleGraphicsModel,
    EvolutionType,
    ItemCategory,
    OutputBattle,
    PlagueType,
    SeenStatus,
)
from tuxemon.item.item import Item
from tuxemon.locale import T
from tuxemon.menu.interface import MenuItem
from tuxemon.monster import Monster
from tuxemon.npc import NPC
from tuxemon.platform.const import buttons
from tuxemon.platform.events import PlayerInput
from tuxemon.session import local_session
from tuxemon.sprite import Sprite
from tuxemon.states.monster import MonsterMenuState
from tuxemon.states.transition.fade import FadeOutTransition
from tuxemon.surfanim import SurfaceAnimation
from tuxemon.technique.technique import Technique
from tuxemon.tools import assert_never
from tuxemon.ui.draw import GraphicBox
from tuxemon.ui.text import TextArea

from .combat_animations import CombatAnimations

logger = logging.getLogger(__name__)

CombatPhase = Literal[
    "begin",
    "ready",
    "housekeeping phase",
    "decision phase",
    "pre action phase",
    "action phase",
    "post action phase",
    "resolve match",
    "ran away",
    "draw match",
    "has winner",
    "end combat",
]


class EnqueuedAction(NamedTuple):
    user: Union[Monster, NPC, None]
    technique: Union[Technique, Item, None]
    target: Monster


# TODO: move to mod config
MULT_MAP = {
    4: "attack_very_effective",
    2: "attack_effective",
    0.5: "attack_resisted",
    0.25: "attack_weak",
}

# This is the time, in seconds, that the text takes to display.
LETTER_TIME: float = 0.02
# This is the time, in seconds, that the animation takes to finish.
ACTION_TIME: float = 3.0


class TechniqueAnimationCache:
    def __init__(self) -> None:
        self._sprites: Dict[Technique, Optional[Sprite]] = {}

    def get(self, technique: Technique, is_flipped: bool) -> Optional[Sprite]:
        """
        Return a sprite usable as a technique animation.

        Parameters:
            technique: Technique whose sprite is requested.
            is_flipped: Flag to determine whether animation frames should be flipped.

        Returns:
            Sprite associated with the technique animation.

        """
        try:
            return self._sprites[technique]
        except KeyError:
            sprite = self.load_technique_animation(technique, is_flipped)
            self._sprites[technique] = sprite
            return sprite

    @staticmethod
    def load_technique_animation(
        technique: Technique, is_flipped: bool
    ) -> Optional[Sprite]:
        """
        Return animated sprite from a technique.

        Parameters:
            technique: Technique whose sprite is requested.
            is_flipped: Flag to determine whether animation frames should be flipped.

        Returns:
            Sprite associated with the technique animation.

        """
        if not technique.images:
            return None
        frame_time = 0.09
        images = list()
        for fn in technique.images:
            image = graphics.load_and_scale(fn)
            images.append((image, frame_time))
        tech = SurfaceAnimation(images, False)
        if is_flipped:
            tech.flip(technique.flip_axes)
        return Sprite(animation=tech)


class WaitForInputState(state.State):
    """Just wait for input blocking everything"""

    def process_event(self, event: PlayerInput) -> Optional[PlayerInput]:
        if event.pressed and event.button == buttons.A:
            self.client.pop_state(self)

        return None


class CombatState(CombatAnimations):
    """The state-menu responsible for all combat related tasks and functions.
        .. image:: images/combat/monster_drawing01.png

    General description of this class:
        * implements a simple state machine
        * various phases are executed using a queue of actions
        * "decision queue" is used to queue player interactions/menus
        * this class holds mostly logic, though some graphical functions exist
        * most graphical functions are contained in "CombatAnimations" class

    Currently, status icons are implemented as follows:
       each round, all status icons are destroyed
       status icons are created for each status on each monster
       obvs, not ideal, maybe someday make it better? (see transition_phase)
    """

    draw_borders = False
    escape_key_exits = False

    def __init__(
        self,
        players: Tuple[NPC, NPC],
        graphics: BattleGraphicsModel,
        combat_type: Literal["monster", "trainer"],
    ) -> None:
        self.max_positions = 1  # TODO: make dependant on match type
        self.phase: Optional[CombatPhase] = None
        self._damage_map: MutableMapping[Monster, Set[Monster]] = defaultdict(
            set
        )
        self._technique_cache = TechniqueAnimationCache()
        self._decision_queue: List[Monster] = []
        self._action_queue: List[EnqueuedAction] = []
        self._log_action: List[Tuple[int, EnqueuedAction]] = []
        # list of sprites that are status icons
        self._status_icons: List[Sprite] = []
        self._monster_sprite_map: MutableMapping[Monster, Sprite] = {}
        self._layout = dict()  # player => home areas on screen
        self._animation_in_progress = False  # if true, delay phase change
        self._turn = 0
        self._prize = 0
        self._lost_status: Optional[str] = None
        self._lost_monster: Optional[Monster] = None

        super().__init__(players, graphics)
        self.is_trainer_battle = combat_type == "trainer"
        self.show_combat_dialog()
        self.transition_phase("begin")
        self.task(partial(setattr, self, "phase", "ready"), 3)

    def update(self, time_delta: float) -> None:
        """
        Update the combat state.  State machine is checked.

        General operation:
        * determine what phase to update
        * if new phase, then run transition into new one
        * update the new phase, or the current one
        """
        super().update(time_delta)
        if not self._animation_in_progress:
            new_phase = self.determine_phase(self.phase)
            if new_phase:
                self.phase = new_phase
                self.transition_phase(new_phase)
            self.update_phase()

    def draw(self, surface: pygame.surface.Surface) -> None:
        """
        Draw combat state.

        Parameters:
            surface: Surface where to draw.

        """
        super().draw(surface)
        self.draw_hp_bars()
        self.draw_exp_bars()

    def draw_hp_bars(self) -> None:
        """Go through the HP bars and redraw them."""
        for monster, hud in self.hud.items():
            rect = Rect(0, 0, tools.scale(70), tools.scale(8))
            rect.right = hud.image.get_width() - tools.scale(8)
            rect.top += tools.scale(12)
            self._hp_bars[monster].draw(hud.image, rect)

    def draw_exp_bars(self) -> None:
        """Go through the EXP bars and redraw them."""
        for monster, hud in self.hud.items():
            if hud.player:
                rect = Rect(0, 0, tools.scale(70), tools.scale(6))
                rect.right = hud.image.get_width() - tools.scale(8)
                rect.top += tools.scale(31)
                self._exp_bars[monster].draw(hud.image, rect)

    def determine_phase(
        self,
        phase: Optional[CombatPhase],
    ) -> Optional[CombatPhase]:
        """
        Determine the next phase and set it.

        Part of state machine
        Only test and set new phase.
        * Do not update phase actions
        * Try not to modify any values
        * Return a phase name and phase will change
        * Return None and phase will not change

        Parameters:
            phase: Current phase of the combat. Could be ``None`` if called
                before the combat had time to start.

        Returns:
            Next phase of the combat.

        """
        if phase is None or phase == "begin":
            return None

        elif phase == "ready":
            return "housekeeping phase"

        elif phase == "housekeeping phase":
            # this will wait for players to fill battleground positions
            for player in self.active_players:
                positions_available = self.max_positions - len(
                    self.monsters_in_play[player]
                )
                if positions_available:
                    return None
            return "decision phase"

        elif phase == "decision phase":
            # TODO: only works for single player and if player runs
            if len(self.remaining_players) == 1:
                return "ran away"

            # assume each monster executes one action
            # if number of actions == monsters, then all monsters are ready
            elif len(self._action_queue) == len(self.active_monsters):
                return "pre action phase"

            return None

        elif phase == "pre action phase":
            return "action phase"

        elif phase == "action phase":
            if not self._action_queue:
                return "post action phase"

            return None

        elif phase == "post action phase":
            if not self._action_queue:
                return "resolve match"

            return None

        elif phase == "resolve match":
            remaining = len(self.remaining_players)

            if remaining == 0:
                return "draw match"
            elif remaining == 1:
                return "has winner"
            else:
                return "housekeeping phase"

        elif phase == "ran away":
            return "end combat"

        elif phase == "draw match":
            return "end combat"

        elif phase == "has winner":
            return "end combat"

        elif phase == "end combat":
            return None

        else:
            assert_never(phase)

    def transition_phase(self, phase: CombatPhase) -> None:
        """
        Change from one phase from another.

        Part of state machine
        * Will be run just -once- when phase changes
        * Do not change phase
        * Execute code only to change into new phase
        * The phase's update will be executed -after- this

        Parameters:
            phase: Name of phase to transition to.

        """
        letter_time = LETTER_TIME
        action_time = ACTION_TIME
        if phase == "begin" or phase == "ready" or phase == "pre action phase":
            pass

        elif phase == "housekeeping phase":
            self._turn += 1
            # reset run for the next turn
            self._run = "on"
            # fill all battlefield positions, but on round 1, don't ask
            self.fill_battlefield_positions(ask=self._turn > 1)

            # plague
            # record the useful properties of the last monster we fought
            monster_record = self.monsters_in_play[self.players[1]][0]
            if self.players[1].plague == PlagueType.infected:
                monster_record.plague = PlagueType.infected
            if monster_record in self.active_monsters:
                var = self.players[0].game_variables
                var["battle_last_monster_name"] = monster_record.name
                var["battle_last_monster_level"] = monster_record.level
                var["battle_last_monster_type"] = monster_record.types[0]
                var["battle_last_monster_category"] = monster_record.category
                var["battle_last_monster_shape"] = monster_record.shape
                # Avoid reset string to seen if monster has already been caught
                if monster_record.slug not in self.players[0].tuxepedia:
                    self.players[0].tuxepedia[
                        monster_record.slug
                    ] = SeenStatus.seen

        elif phase == "decision phase":
            self.reset_status_icons()
            # saves random value, so we are able to reproduce
            # inside the condition files if a tech hit or missed
            value = random.random()
            self.players[0].game_variables["random_tech_hit"] = value
            if not self._decision_queue:
                for player in self.human_players:
                    # tracks human players who need to choose an action
                    self._decision_queue.extend(self.monsters_in_play[player])

                for trainer in self.ai_players:
                    for monster in self.monsters_in_play[trainer]:
                        self.get_combat_decision_from_ai(monster)
                        # recharge opponent moves
                        for tech in monster.moves:
                            tech.recharge()

        elif phase == "action phase":
            self.sort_action_queue()

        elif phase == "post action phase":
            # apply status effects to the monsters
            for monster in self.active_monsters:
                for technique in monster.status:
                    # validate status
                    if technique.validate(monster):
                        technique.combat_state = self
                        # update counter nr turns
                        technique.nr_turn += 1
                        self.enqueue_action(None, technique, monster)
                    # avoid multiple effect status
                    monster.set_stats()

        elif phase == "resolve match":
            pass

        elif phase == "ran away":
            self.players[0].set_party_status()
            var = self.players[0].game_variables
            if self.is_trainer_battle:
                var["battle_last_result"] = OutputBattle.forfeit
                var["teleport_clinic"] = OutputBattle.lost
                message = T.format(
                    "combat_forfeit",
                    {
                        "npc": self.players[1].name,
                    },
                )
            else:
                # remove monsters still around
                for mon in self.ai_players:
                    mon.monsters.pop()
                var["battle_last_result"] = OutputBattle.ran
                message = T.translate("combat_player_run")

            # push a state that blocks until enter is pressed
            # after the state is popped, the combat state will clean up and close
            # if you run in PvP, you need "defeated message"
            self.alert(message)
            action_time += len(message) * letter_time
            self.task(
                partial(self.client.push_state, WaitForInputState()),
                action_time,
            )
            self.suppress_phase_change(action_time)

        elif phase == "draw match":
            self.players[0].set_party_status()
            var = self.players[0].game_variables
            var["battle_last_result"] = OutputBattle.draw
            var["teleport_clinic"] = OutputBattle.lost
            if self.is_trainer_battle:
                var["battle_last_trainer"] = self.players[1].slug
                # track battles against NPC
                battle = Battle()
                battle.opponent = self.players[1].slug
                battle.outcome = OutputBattle.draw
                battle.date = dt.date.today().toordinal()
                self.players[0].battles.append(battle)

            # it is a draw match; both players were defeated in same round
            message = T.translate("combat_draw")
            self.alert(message)
            action_time += len(message) * letter_time

            # push a state that blocks until enter is pressed
            # after the state is popped, the combat state will clean up and close
            self.task(
                partial(self.client.push_state, WaitForInputState()),
                action_time,
            )
            self.suppress_phase_change(action_time)

        elif phase == "has winner":
            # TODO: proper match check, etc
            # This assumes that player[0] is the human playing in single player
            self.players[0].set_party_status()
            var = self.players[0].game_variables
            if self.remaining_players[0] == self.players[0]:
                var["battle_last_result"] = OutputBattle.won
                if self.is_trainer_battle:
                    message = T.format(
                        "combat_victory_trainer",
                        {
                            "npc": self.players[1].name,
                            "prize": self._prize,
                            "currency": "$",
                        },
                    )
                    self.players[0].give_money(self._prize)
                    var["battle_last_trainer"] = self.players[1].slug
                    # track battles against NPC
                    battle = Battle()
                    battle.opponent = self.players[1].slug
                    battle.outcome = OutputBattle.won
                    battle.date = dt.date.today().toordinal()
                    self.players[0].battles.append(battle)
                else:
                    message = T.translate("combat_victory")

            else:
                var["battle_last_result"] = OutputBattle.lost
                var["teleport_clinic"] = OutputBattle.lost
                message = T.translate("combat_defeat")
                if self.is_trainer_battle:
                    var["battle_last_trainer"] = self.players[1].slug
                    # track battles against NPC
                    battle = Battle()
                    battle.opponent = self.players[1].slug
                    battle.outcome = OutputBattle.lost
                    battle.date = dt.date.today().toordinal()
                    self.players[0].battles.append(battle)

            # push a state that blocks until enter is pressed
            # after the state is popped, the combat state will clean up and close
            self.alert(message)
            action_time += len(message) * letter_time
            self.task(
                partial(self.client.push_state, WaitForInputState()),
                action_time,
            )
            self.suppress_phase_change(action_time)

        elif phase == "end combat":
            self.players[0].set_party_status()
            self.end_combat()
            self.evolve()

        else:
            assert_never(phase)

    def get_combat_decision_from_ai(self, monster: Monster) -> None:
        """
        Get ai action from a monster and enqueue it.

        Parameters:
            monster: Monster whose action will be decided.

        Returns:
            Enqueued action of the monster.

        """
        # TODO: parties/teams/etc to choose opponents
        opponents = self.monsters_in_play[self.players[0]]
        trainer = self.players[1]
        assert monster.ai
        if self.is_trainer_battle:
            user, technique, target = monster.ai.make_decision_trainer(
                trainer, monster, opponents
            )
        else:
            user, technique, target = monster.ai.make_decision_wild(
                trainer, monster, opponents
            )
        # check status response
        if isinstance(user, Monster) and isinstance(technique, Technique):
            technique = pre_checking(
                user, technique, target, self.players[0], self.players[0]
            )
            # check status response
            if self.status_response_technique(user, technique):
                self._lost_monster = user
        if isinstance(user, NPC) and isinstance(technique, Item):
            if self.status_response_item(target):
                self._lost_monster = target
        action = EnqueuedAction(user, technique, target)
        self._action_queue.append(action)
        self._log_action.append((self._turn, action))

    def sort_action_queue(self) -> None:
        """Sort actions in the queue according to game rules.

        * Swap actions are always first
        * Techniques that damage are sorted by monster speed
        * Items are sorted by trainer speed

        """

        def rank_action(action: EnqueuedAction) -> Tuple[int, int]:
            if action.technique is None:
                return 0, 0
            sort = action.technique.sort
            primary_order = sort_order.index(sort)

            if sort == "meta":
                # all meta items sorted together
                # use of 0 leads to undefined sort/probably random
                return primary_order, 0
            elif sort == "potion":
                return primary_order, 0
            else:
                # TODO: determine the secondary sort element,
                # monster speed, trainer speed, etc
                assert action.user
                return primary_order, action.user.speed_test(action)

        # TODO: move to mod config
        sort_order = [
            "meta",
            "item",
            "utility",
            "potion",
            "food",
            "heal",
            "damage",
        ]

        # TODO: Running happens somewhere else, it should be moved here
        # i think.
        # TODO: Eventually make an action queue class?
        self._action_queue.sort(key=rank_action, reverse=True)

    def update_phase(self) -> None:
        """
        Execute/update phase actions.

        Part of state machine
        * Do not change phase
        * Will be run each iteration phase is active
        * Do not test conditions to change phase

        """
        if self.phase == "decision phase":
            # show monster action menu for human players
            if self._decision_queue:
                monster = self._decision_queue.pop()
                for tech in monster.moves:
                    tech.recharge()
                self.show_monster_action_menu(monster)

        elif self.phase == "action phase":
            self.handle_action_queue()

        elif self.phase == "post action phase":
            self.handle_action_queue()

    def handle_action_queue(self) -> None:
        """Take one action from the queue and do it."""
        if self._action_queue:
            action = self._action_queue.pop()
            self.perform_action(*action)
            self.task(self.check_party_hp, 1)
            self.task(self.animate_party_status, 3)

    def ask_player_for_monster(self, player: NPC) -> None:
        """
        Open dialog to allow player to choose a Tuxemon to enter into play.

        Parameters:
            player: Player who has to select a Tuxemon.

        """

        def add(menuitem: MenuItem[Monster]) -> None:
            monster = menuitem.game_object
            if monster.current_hp == 0:
                tools.open_dialog(
                    local_session,
                    [
                        T.format(
                            "combat_fainted",
                            parameters={"name": monster.name},
                        ),
                    ],
                )
            elif monster in self.active_monsters:
                tools.open_dialog(
                    local_session,
                    [
                        T.format(
                            "combat_isactive",
                            parameters={"name": monster.name},
                        ),
                    ],
                )
                msg = T.translate("combat_replacement_is_fainted")
                tools.open_dialog(local_session, [msg])
            else:
                self.add_monster_into_play(player, monster)
                self.client.pop_state()

        state = self.client.push_state(MonsterMenuState())
        # must use a partial because alert relies on a text box that may not
        # exist until after the state hs been startup
        state.task(partial(state.alert, T.translate("combat_replacement")), 0)
        state.on_menu_selection = add  # type: ignore[assignment]
        state.escape_key_exits = False

    def fill_battlefield_positions(self, ask: bool = False) -> None:
        """
        Check the battlefield for unfilled positions and send out monsters.

        Parameters:
            ask: If True, then open dialog for human players.

        """
        # TODO: let work for trainer battles
        humans = list(self.human_players)

        # TODO: integrate some values for different match types
        released = False
        for player in self.active_players:
            positions_available = self.max_positions - len(
                self.monsters_in_play[player]
            )
            if positions_available:
                available = get_awake_monsters(
                    player, self.monsters_in_play[player]
                )
                for _ in range(positions_available):
                    released = True
                    if player in humans and ask:
                        self.ask_player_for_monster(player)
                    else:
                        self.add_monster_into_play(player, next(available))

        if released:
            self.suppress_phase_change()

    def add_monster_into_play(
        self,
        player: NPC,
        monster: Monster,
        removed: Union[Monster, None] = None,
    ) -> None:
        """
        Add a monster to the battleground.

        Parameters:
            player: Player who adds the monster, if any.
            monster: Added monster.

        """
        # TODO: refactor some into the combat animations
        self.animate_monster_release(player, monster)
        self.build_hud(self._layout[player]["hud"][0], monster)
        self.monsters_in_play[player].append(monster)

        # remove "connected" status (eg. lifeleech, etc.)
        for mon in self.active_monsters:
            if has_status_bond(mon):
                mon.status.clear()

        # TODO: not hardcode
        message = T.format(
            "combat_swap",
            {
                "target": monster.name.upper(),
                "user": player.name.upper(),
            },
        )
        if removed and has_status(removed, "status_harpooned"):
            removed.current_hp -= removed.hp // 8
            if removed.current_hp <= 0:
                faint = Technique()
                faint.load("status_faint")
                monster.current_hp = 0
                if monster.status:
                    monster.status[0].nr_turn = 0
                monster.status = [faint]
        self.alert(message)
        # save iid monster fighting
        if player is self.players[0]:
            self.players[0].game_variables["iid_fighting_monster"] = str(
                monster.instance_id.hex
            )
        elif self.is_trainer_battle:
            pass
        else:
            message = T.format(
                "combat_wild_appeared",
                {"name": monster.name.upper()},
            )
            self.alert(message)

    def reset_status_icons(self) -> None:
        """
        Update/reset status icons for monsters.

        TODO: caching, etc
        """
        # remove all status icons
        for s in self._status_icons:
            self.sprites.remove(s)

        # add status icons
        for monster in self.active_monsters:
            for status in monster.status:
                if status.icon:
                    status_ico: Tuple[float, float] = (0.0, 0.0)
                    if monster in self.players[1].monsters:
                        status_ico = (
                            self.rect.width * 0.06,
                            self.rect.height * 0.12,
                        )
                    elif monster in self.players[0].monsters:
                        status_ico = (
                            self.rect.width * 0.64,
                            self.rect.height * 0.52,
                        )
                    # load the sprite and add it to the display
                    icon = self.load_sprite(
                        status.icon,
                        layer=200,
                        center=status_ico,
                    )
                    self._status_icons.append(icon)

    def show_combat_dialog(self) -> None:
        """Create and show the area where battle messages are displayed."""
        # make the border and area at the bottom of the screen for messages
        rect_screen = self.client.screen.get_rect()
        rect = Rect(0, 0, rect_screen.w, rect_screen.h // 4)
        rect.bottomright = rect_screen.w, rect_screen.h
        border = graphics.load_and_scale(self.borders_filename)
        self.dialog_box = GraphicBox(border, None, self.background_color)
        self.dialog_box.rect = rect
        self.sprites.add(self.dialog_box, layer=100)

        # make a text area to show messages
        self.text_area = TextArea(self.font, self.font_color)
        self.text_area.rect = self.dialog_box.calc_inner_rect(
            self.dialog_box.rect,
        )
        self.sprites.add(self.text_area, layer=100)

    def show_monster_action_menu(self, monster: Monster) -> None:
        """
        Show the main window for choosing player actions.

        Parameters:
            monster: Monster to choose an action for.

        """
        from tuxemon.states.combat.combat_menus import MainCombatMenuState

        message = T.format("combat_monster_choice", {"name": monster.name})
        self.alert(message)
        rect_screen = self.client.screen.get_rect()
        rect = Rect(0, 0, rect_screen.w // 2.5, rect_screen.h // 4)
        rect.bottomright = rect_screen.w, rect_screen.h

        state = self.client.push_state(
            MainCombatMenuState(
                monster=monster,
                player=self.players[0],
                enemy=self.players[1],
            )
        )
        state.rect = rect

    def enqueue_action(
        self,
        user: Union[NPC, Monster, None],
        technique: Union[Item, Technique, None],
        target: Monster,
    ) -> None:
        """
        Add some technique or status to the action queue.

        Parameters:
            user: The user of the technique.
            technique: The technique used.
            target: The target of the action.

        """
        action = EnqueuedAction(user, technique, target)
        self._action_queue.append(action)
        self._log_action.append((self._turn, action))

    def rewrite_action_queue_target(
        self,
        original: Monster,
        new: Monster,
    ) -> None:
        """
        Used for swapping monsters.

        Parameters:
            original: Original targeted monster.
            new: New monster.

        """
        # rewrite actions in the queue to target the new monster
        for index, action in enumerate(self._action_queue):
            if action.target is original:
                new_action = EnqueuedAction(action.user, action.technique, new)
                self._action_queue[index] = new_action

    def remove_monster_from_play(
        self,
        monster: Monster,
    ) -> None:
        """
        Remove monster from play without fainting it.

        * If another monster has targeted this monster, it can change action
        * Will remove actions as well
        * currently for 'swap' technique

        """
        self.remove_monster_actions_from_queue(monster)
        self.animate_monster_faint(monster)

    def remove_monster_actions_from_queue(self, monster: Monster) -> None:
        """
        Remove all queued actions for a particular monster.

        This is used mainly for removing actions after monster is fainted.

        Parameters:
            monster: Monster whose actions will be removed.

        """
        to_remove = set()
        for action in self._action_queue:
            if action.user is monster or action.target is monster:
                to_remove.add(action)

        for action in to_remove:
            self._action_queue.remove(action)

    def suppress_phase_change(
        self,
        delay: float = 3.0,
    ) -> Optional[Task]:
        """
        Prevent the combat phase from changing for a limited time.

        Use this function to prevent the phase from changing.  When
        animating elements of the phase, call this to prevent player
        input as well as phase changes.

        Parameters:
            delay: Amount of seconds to delay phase changes.

        """
        if self._animation_in_progress:
            logger.debug("double suppress: bug?")
            return None

        self._animation_in_progress = True
        return self.task(
            partial(setattr, self, "_animation_in_progress", False),
            delay,
        )

    def perform_action(
        self,
        user: Union[Monster, NPC, None],
        technique: Union[Technique, Item, None],
        target: Monster,
    ) -> None:
        """
        Perform the action.

        Parameters:
            user: Monster that does the action.
            technique: Technique or item used.
            target: Monster that receives the action.

        """
        _player = local_session.player
        letter_time = LETTER_TIME
        action_time = ACTION_TIME
        # action is performed, so now use sprites to animate it
        # this value will be None if the target is off screen
        target_sprite = self._monster_sprite_map.get(target, None)
        # slightly delay the monster shake, so technique animation
        # is synchronized with the damage shake motion
        hit_delay = 0.0
        # monster uses move
        if isinstance(technique, Technique) and isinstance(user, Monster):
            technique.advance_round()
            result_tech = technique.use(user, target)
            context = {
                "user": user.name,
                "name": technique.name,
                "target": target.name,
            }
            message = T.format(technique.use_tech, context)
            # scope technique
            if technique.slug == "scope":
                message = scope(target)
            # swapping monster
            if technique.slug == "swap":
                message = T.format(
                    "combat_call_tuxemon",
                    {"name": target.name.upper()},
                )
            # null action dozing monster
            if technique.slug == "empty" and has_status(user, "status_dozing"):
                message = T.format(
                    "combat_state_dozing_end",
                    {
                        "target": user.name.upper(),
                    },
                )
            # confused status
            if (
                has_status(user, "status_confused")
                and "status_confused" in _player.game_variables
            ):
                if _player.game_variables["status_confused"] == "on":
                    message = confused(user, technique)
            # successful techniques
            if result_tech["success"]:
                m: Union[str, None] = None
                # related to switch effect
                if has_effect(technique, "switch"):
                    m = generic(user, technique, target, _player)
                if m:
                    message += "\n" + m
                    action_time += len(message) * letter_time
            # not successful techniques
            if not result_tech["success"]:
                template = getattr(technique, "use_failure")
                m = T.format(template, context)
                if technique.slug == "spyderbite":
                    m = spyderbite(target)
                if has_effect(technique, "money"):
                    m = generic(user, technique, target, _player)
                # related to healing effect
                if has_effect(technique, "healing"):
                    m = generic(user, technique, target, _player)
                message += "\n" + m
                action_time += len(message) * letter_time
            # TODO: caching sounds
            audio.load_sound(technique.sfx, None).play()
            # animation own monster AI NPC
            if "own monster" in technique.target:
                target_sprite = self._monster_sprite_map.get(user, None)
            # TODO: a real check or some params to test if should tackle, etc
            if result_tech["should_tackle"]:
                hit_delay += 0.5
                user_sprite = self._monster_sprite_map[user]
                self.animate_sprite_tackle(user_sprite)

                if target_sprite:
                    self.task(
                        partial(
                            self.animate_sprite_take_damage,
                            target_sprite,
                        ),
                        hit_delay + 0.2,
                    )
                    self.task(
                        partial(self.blink, target_sprite),
                        hit_delay + 0.6,
                    )

                # TODO: track total damage
                # Track damage
                self._damage_map[target].add(user)

                # monster infected
                if user.plague == PlagueType.infected:
                    m = T.format(
                        "combat_state_plague1",
                        {
                            "target": user.name.upper(),
                        },
                    )
                    message += "\n" + m
                # allows tackle to special range techniques too
                if technique.range != "special":
                    element_damage_key = MULT_MAP.get(
                        result_tech["element_multiplier"]
                    )
                    if element_damage_key:
                        m = T.translate(element_damage_key)
                        message += "\n" + m
                        action_time += len(message) * letter_time
                # if the monster lost status due technique
                if self._lost_monster == user:
                    if self._lost_status:
                        message += "\n" + self._lost_status
                        action_time += len(message) * letter_time
                    self._lost_status = None
                    self._lost_monster = None
                else:
                    msg_type = (
                        "use_success"
                        if result_tech["success"]
                        else "use_failure"
                    )
                    context = {
                        "user": getattr(user, "name", ""),
                        "name": technique.name,
                        "target": target.name,
                    }
                    template = getattr(technique, msg_type)
                    tmpl = T.format(template, context)
                    if template:
                        message += "\n" + tmpl
                        action_time += len(message) * letter_time

            self.alert(message)
            self.suppress_phase_change(action_time)

            is_flipped = False
            for trainer in self.ai_players:
                if user in self.monsters_in_play[trainer]:
                    is_flipped = True
                    break
            tech_sprite = self._technique_cache.get(technique, is_flipped)

            if result_tech["success"] and target_sprite and tech_sprite:
                tech_sprite.rect.center = target_sprite.rect.center
                assert tech_sprite.animation
                self.task(tech_sprite.animation.play, hit_delay)
                self.task(
                    partial(self.sprites.add, tech_sprite, layer=50),
                    hit_delay,
                )
                self.task(tech_sprite.kill, action_time)

        # player uses item
        if isinstance(technique, Item) and isinstance(user, NPC):
            result_item = technique.use(user, target)
            context = {
                "user": user.name,
                "name": technique.name,
                "target": target.name,
            }
            message = T.format(technique.use_item, context)
            # handle the capture device
            if technique.category == ItemCategory.capture:
                # retrieve tuxeball
                message += "\n" + T.translate("attempting_capture")
                action_time = result_item["num_shakes"] + 1.8
                self.animate_capture_monster(
                    result_item["success"],
                    result_item["num_shakes"],
                    target,
                    technique,
                    self,
                )
            else:
                msg_type = (
                    "use_success" if result_item["success"] else "use_failure"
                )
                context = {
                    "user": getattr(user, "name", ""),
                    "name": technique.name,
                    "target": target.name,
                }
                template = getattr(technique, msg_type)
                tmpl = T.format(template, context)
                # check status festering, potions = no healing
                if technique.category == ItemCategory.potion and has_status(
                    target, "status_festering"
                ):
                    tmpl = T.translate("combat_state_festering_item")
                if template:
                    message += "\n" + tmpl
                    action_time += len(message) * letter_time

            self.alert(message)
            self.suppress_phase_change(action_time)
        # statuses / conditions
        if user is None and isinstance(technique, Technique):
            result = technique.use(None, target)
            if result["success"]:
                msg_type = (
                    "use_success" if result["success"] else "use_failure"
                )
                context = {
                    "name": technique.name,
                    "target": target.name,
                }
                template = getattr(technique, msg_type)
                message = T.format(template, context)
                # first turn status
                mex: str = " "
                msg_type = "use_tech"
                template = getattr(technique, msg_type)
                if technique.nr_turn == 1 and template != mex:
                    message = mex + "\n" + message
                action_time += len(message) * letter_time
                self.alert(message)
                self.suppress_phase_change(action_time)

            # effect animation
            is_flipped = False
            tech_sprite = self._technique_cache.get(technique, is_flipped)
            if target_sprite and tech_sprite:
                tech_sprite.rect.center = target_sprite.rect.center
                assert tech_sprite.animation
                self.task(tech_sprite.animation.play, 0.6)
                self.task(
                    partial(self.sprites.add, tech_sprite, layer=50),
                    0.6,
                )
                self.task(tech_sprite.kill, action_time)

    def faint_monster(self, monster: Monster) -> None:
        """
        Instantly make the monster faint (will be removed later).

        Parameters:
            monster: Monster that will faint.

        """
        faint = Technique()
        faint.load("status_faint")
        monster.current_hp = 0
        if monster.status:
            monster.status[0].nr_turn = 0
        monster.status = [faint]

        """
        Experience is earned when the target monster is fainted.
        Any monsters who contributed any amount of damage will be awarded.
        Experience is distributed evenly to all participants.
        """
        message: str = ""
        action_time = ACTION_TIME
        letter_time = LETTER_TIME
        if monster in self._damage_map:
            # Award Experience
            awarded_exp = (
                monster.total_experience
                // (monster.level * len(self._damage_map[monster]))
            ) * monster.experience_modifier
            awarded_mon = monster.level * monster.money_modifier
            for winners in self._damage_map[monster]:
                # check before giving exp
                self._level_before = winners.level
                winners.give_experience(awarded_exp)
                # check after giving exp
                self._level_after = winners.level
                if self.is_trainer_battle:
                    self._prize += awarded_mon
                # it checks if there is a "level up"
                if self._level_before != self._level_after:
                    diff = self._level_after - self._level_before
                    if winners in self.players[0].monsters:
                        # checks and eventually teaches move/moves
                        mex = check_moves(
                            self.monsters_in_play[self.players[0]][0], diff
                        )
                        if mex:
                            message += "\n" + mex
                            action_time += len(message) * letter_time
                        # updates hud graphics player
                        self.build_hud(
                            self._layout[self.players[0]]["hud"][0],
                            self.monsters_in_play[self.players[0]][0],
                        )
                if winners in self.players[0].monsters:
                    m = T.format(
                        "combat_gain_exp",
                        {"name": winners.name.upper(), "xp": awarded_exp},
                    )
                    message += "\n" + m
            action_time += len(message) * letter_time
            self.alert(message)
            self.suppress_phase_change(action_time)

            # Remove monster from damage map
            del self._damage_map[monster]

    def animate_party_status(self) -> None:
        """
        Animate monsters that need to be fainted.

        * Animation to remove monster is handled here
        TODO: check for faint status, not HP

        """
        for _, party in self.monsters_in_play.items():
            for monster in party:
                if fainted(monster):
                    self.alert(
                        T.format(
                            "combat_fainted",
                            {"name": monster.name},
                        ),
                    )
                    self.animate_monster_faint(monster)
                    self.suppress_phase_change()

    def check_party_hp(self) -> None:
        """
        Apply status effects, then check HP, and party status.

        * Monsters will be removed from play here

        """
        for _, party in self.monsters_in_play.items():
            for monster in party:
                self.animate_hp(monster)
                # check for recover (completely healed)
                if monster.current_hp >= monster.hp and has_status(
                    monster, "status_recover"
                ):
                    monster.status = []
                    # avoid "overcome" hp bar
                    if monster.current_hp > monster.hp:
                        monster.current_hp = monster.hp
                    self.alert(
                        T.format(
                            "combat_state_recover_failure",
                            {
                                "target": monster.name.upper(),
                            },
                        )
                    )
                # check for condition diehard
                if monster.current_hp <= 0 and has_status(
                    monster, "status_diehard"
                ):
                    monster.current_hp = 1
                    monster.status = []
                    self.alert(
                        T.format(
                            "combat_state_diehard_tech",
                            {
                                "target": monster.name.upper(),
                            },
                        )
                    )
                if monster.current_hp <= 0 and not has_status(
                    monster, "status_faint"
                ):
                    self.remove_monster_actions_from_queue(monster)
                    self.faint_monster(monster)

                    # If a monster fainted, exp was given, thus the exp bar
                    # should be updated
                    # The exp bar must only be animated for the player's
                    # monsters
                    # Enemies don't have a bar, doing it for them will
                    # cause a crash
                    for monster in self.monsters_in_play[local_session.player]:
                        self.task(partial(self.animate_exp, monster), 2.5)
                        self.suppress_phase_change()

    @property
    def active_players(self) -> Iterable[NPC]:
        """
        Generator of any non-defeated players/trainers.

        Returns:
            Iterable with active players.

        """
        for player in self.players:
            if not defeated(player):
                yield player

    @property
    def human_players(self) -> Iterable[NPC]:
        for player in self.players:
            if player.isplayer:
                yield player

    @property
    def ai_players(self) -> Iterable[NPC]:
        yield from set(self.active_players) - set(self.human_players)

    @property
    def active_monsters(self) -> Sequence[Monster]:
        """
        List of any non-defeated monsters on battlefield.

        Returns:
            Sequence of active monsters.

        """
        return list(chain.from_iterable(self.monsters_in_play.values()))

    @property
    def remaining_monsters(self) -> Sequence[Monster]:
        """
        List of any non-fainted monsters in party (human).

        """
        return [p for p in self.players[0].monsters if not fainted(p)]

    @property
    def remaining_monsters_ai(self) -> Sequence[Monster]:
        """
        List of any non-fainted monsters in party (ai).

        """
        return [p for p in self.players[1].monsters if not fainted(p)]

    @property
    def remaining_players(self) -> Sequence[NPC]:
        """
        List of non-defeated players/trainers. WIP.

        Right now, this is similar to Combat.active_players, but it may change
        in the future.
        For implementing teams, this would need to be different than
        active_players.

        Use to check for match winner.

        Returns:
            Sequence of remaining players.

        """
        # TODO: perhaps change this to remaining "parties", or "teams",
        # instead of player/trainer
        return [p for p in self.players if not defeated(p)]

    def status_response_item(self, monster: Monster) -> bool:
        # change charging -> charged up
        if has_status(monster, "status_charging"):
            monster.status.clear()
            status = Technique()
            status.load("status_chargedup")
            monster.apply_status(status)
            return True
        return False

    def status_response_technique(
        self, monster: Monster, technique: Technique
    ) -> bool:
        """Checks the technique used and its status response:
        - eventually removes the status
        - eventually shows a text
        """
        # removes enraged
        if has_status(monster, "status_enraged") and not has_effect_param(
            technique, "status_enraged", "give", "condition"
        ):
            monster.status.clear()
            return True
        # removes sniping
        if has_status(monster, "status_sniping") and not has_effect_param(
            technique, "status_sniping", "give", "condition"
        ):
            monster.status.clear()
            return True
        # removes dozing
        if has_status(monster, "status_dozing"):
            monster.status.clear()
            return True
        # removes tired
        if has_status(monster, "status_tired"):
            monster.status.clear()
            label = T.format(
                "combat_state_tired_end",
                {
                    "target": monster.name.upper(),
                },
            )
            self._lost_status = label
            return True
        # change exhausted -> tired
        if has_status(monster, "status_exhausted") and not has_effect_param(
            technique, "status_exhausted", "give", "condition"
        ):
            monster.status.clear()
            status = Technique()
            status.load("status_tired")
            monster.apply_status(status)
            return True
        # change charging -> charged up
        if has_status(monster, "status_charging") and not has_effect_param(
            technique, "status_charging", "give", "condition"
        ):
            monster.status.clear()
            status = Technique()
            status.load("status_chargedup")
            monster.apply_status(status)
            return True
        # change charged up -> exhausted
        if has_status(monster, "status_chargedup") and not has_effect_param(
            technique, "status_chargedup", "give", "condition"
        ):
            monster.status.clear()
            status = Technique()
            status.load("status_exhausted")
            monster.apply_status(status)
            return True
        # change nodding off -> dozing
        if has_status(monster, "status_noddingoff") and not has_effect_param(
            technique, "status_noddingoff", "give", "condition"
        ):
            monster.status.clear()
            status = Technique()
            status.load("status_dozing")
            monster.apply_status(status)
            return True
        return False

    def evolve(self) -> None:
        self.client.pop_state()
        for monster in self.players[0].monsters:
            for evolution in monster.evolutions:
                evolved = Monster()
                evolved.load_from_db(evolution.monster_slug)
                if evolution.path == EvolutionType.standard:
                    if evolution.at_level <= monster.level:
                        self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.gender:
                    if evolution.at_level <= monster.level:
                        if evolution.gender == monster.gender:
                            self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.element:
                    if evolution.at_level <= monster.level:
                        if self.players[0].has_type(evolution.element):
                            self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.tech:
                    if evolution.at_level <= monster.level:
                        if self.players[0].has_tech(evolution.tech):
                            self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.location:
                    if evolution.at_level <= monster.level:
                        if evolution.inside == self.client.map_inside:
                            self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.stat:
                    if evolution.at_level <= monster.level:
                        if monster.return_stat(
                            evolution.stat1
                        ) >= monster.return_stat(evolution.stat2):
                            self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.season:
                    if evolution.at_level <= monster.level:
                        if (
                            evolution.season
                            == self.players[0].game_variables["season"]
                        ):
                            self.question_evolution(monster, evolved)
                elif evolution.path == EvolutionType.daytime:
                    if evolution.at_level <= monster.level:
                        if (
                            evolution.daytime
                            == self.players[0].game_variables["daytime"]
                        ):
                            self.question_evolution(monster, evolved)

    def question_evolution(self, monster: Monster, evolved: Monster) -> None:
        tools.open_dialog(
            local_session,
            [
                T.format(
                    "evolution_confirmation",
                    {
                        "name": monster.name.upper(),
                        "evolve": evolved.name.upper(),
                    },
                )
            ],
        )
        tools.open_choice_dialog(
            local_session,
            menu=(
                (
                    "yes",
                    T.translate("yes"),
                    partial(
                        self.positive_answer,
                        monster,
                        evolved,
                    ),
                ),
                (
                    "no",
                    T.translate("no"),
                    self.negative_answer,
                ),
            ),
        )

    def positive_answer(self, monster: Monster, evolved: Monster) -> None:
        self.client.pop_state()
        self.client.pop_state()
        self.players[0].evolve_monster(monster, evolved.slug)

    def negative_answer(self) -> None:
        self.client.pop_state()
        self.client.pop_state()

    def clean_combat(self) -> None:
        """Clean combat."""
        for player in self.players:
            for mon in player.monsters:
                # reset status stats
                mon.set_stats()
                mon.end_combat()
                # reset type
                mon.return_types()
                # reset technique stats
                for tech in mon.moves:
                    tech.set_stats()

        # clear action queue
        self._action_queue = list()
        self._log_action = list()

    def end_combat(self) -> None:
        """End the combat."""
        self.clean_combat()

        # fade music out
        self.client.event_engine.execute_action("fadeout_music", [1000])

        # remove any menus that may be on top of the combat state
        while self.client.current_state is not self:
            self.client.pop_state()

        self.client.push_state(FadeOutTransition(caller=self))
