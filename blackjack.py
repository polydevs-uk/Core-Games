# Game blackjack.py (Có sử dụng claude để code cho nhanh)
# Có tách UI và Logic 
# Src tương đối mở nên UI làm qua loa

from dataclasses import dataclass, asdict
from typing import List, Tuple, Dict, Optional, Any
import random
import time
import json

# --- Optional Discord UI ---
try:
    import discord
    from discord.ext import commands
    from discord import ui, ButtonStyle, Embed
    DISCORD_AVAILABLE = True
except Exception:
    DISCORD_AVAILABLE = False

# =========================
# UI: Discord Cog + View
# =========================
if DISCORD_AVAILABLE:
    class BJView(ui.View):
        """
        View có 2 nút: Hit (Bốc) và Stand (Dừng).
        Chỉ chủ ván (owner_id) được thao tác.
        """
        def __init__(self, owner_id: int, timeout: float = 300.0):
            super().__init__(timeout=timeout)
            self.owner_id = int(owner_id)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.owner_id:
                try:
                    await interaction.response.send_message("❌ Đây không phải ván của bạn.", ephemeral=True)
                except Exception:
                    pass
                return False
            return True

        @ui.button(label="🃏 Bốc", style=ButtonStyle.primary)
        async def hit(self, interaction: discord.Interaction, button: ui.Button):
            """Callback sẽ được gắn tại runtime bởi BlackjackUI (tham chiếu core)."""
            await interaction.response.defer()

        @ui.button(label="🛑 Dừng", style=ButtonStyle.danger)
        async def stand(self, interaction: discord.Interaction, button: ui.Button):
            """Callback sẽ được gắn tại runtime bởi BlackjackUI (tham chiếu core)."""
            await interaction.response.defer()

    class BlackjackUI(commands.Cog):
        """
        Cog Discord tiện lợi. Khởi tạo với:
          BlackjackUI(bot, session_manager=None)
        Nếu session_manager không truyền -> sử dụng SessionManager() mặc định (in-memory).
        """
        def __init__(self, bot, session_manager=None):
            self.bot = bot
            from types import MethodType
            self.manager = session_manager or SessionManager()
            # core instance (no stateful data except helper methods)
            self.core = BlackjackCore()
            # Bind callbacks for view at runtime (so View can call core functions)
            # We'll patch view callbacks dynamically when sending message.

        @commands.command(name="bj")
        async def bj_cmd(self, ctx: commands.Context, bet_token: Optional[str] = None):
            """
            Lệnh: .bj <số tiền> hoặc .bj all
            Lưu ý: module core KHÔNG thay đổi ví/ngân hàng. Nó trả kết quả (win/lose/tie).
            Caller chịu trách nhiệm cập nhật tiền khi cần.
            """
            if not bet_token:
                return await ctx.reply("Cách dùng: `.bj <số tiền>` hoặc `.bj all`", mention_author=False)
            # parse bet
            bt = bet_token.strip().lower()
            bet = self.core.MAX_BET if bt == "all" else None
            if bet is None:
                try:
                    bet = int(float(bt))
                except Exception:
                    return await ctx.reply("Số tiền không hợp lệ.", mention_author=False)
            if bet <= 0:
                return await ctx.reply("Số tiền phải lớn hơn 0.", mention_author=False)
            if bet > self.core.MAX_BET:
                bet = self.core.MAX_BET

            owner_id = ctx.author.id
            # Avoid duplicate session
            if self.manager.get_session(owner_id) is not None:
                return await ctx.reply("Bạn đã có ván đang chơi.", mention_author=False)

            # tạo session core
            session = self.core.create_session(owner_id, bet)
            # lưu session
            self.manager.set_session(owner_id, session)

            # build embed (dealer second card hidden)
            embed = self._build_embed(session, reveal_dealer=False, author=ctx.author)
            view = BJView(owner_id)
            # patch view callbacks to call our methods (closure captures ctx/message)
            async def _on_hit(interaction):
                sess = self.manager.get_session(owner_id)
                if not sess:
                    await interaction.response.send_message("Ván không tồn tại.", ephemeral=True)
                    return
                card = self.core.player_hit(sess)
                # if busted -> finalize
                if sess.player_value > 21:
                    # finalize dealer state
                    self.core.finalize_session(sess)
                    outcome, amt_text = self.core.evaluate(sess)
                    final_embed = self._build_embed(sess, reveal_dealer=True, author=ctx.author)
                    final_embed.add_field(name="Kết quả", value=f"{outcome}\n{amt_text}", inline=False)
                    view.disable_all_items()
                    # update message
                    try:
                        await interaction.message.edit(embed=final_embed, view=view)
                    except Exception:
                        pass
                    self.manager.pop_session(owner_id)
                    await interaction.response.send_message(f"Bạn bốc: {card_to_label(card)} — Bạn bị quá 21.", ephemeral=True)
                    return
                # else update
                embed_upd = self._build_embed(sess, reveal_dealer=False, author=ctx.author)
                try:
                    await interaction.message.edit(embed=embed_upd, view=view)
                except Exception:
                    pass
                await interaction.response.send_message(f"Bạn bốc: {card_to_label(card)} (Tổng: {sess.player_value})", ephemeral=True)

            async def _on_stand(interaction):
                sess = self.manager.get_session(owner_id)
                if not sess:
                    await interaction.response.send_message("Ván không tồn tại.", ephemeral=True)
                    return
                self.core.dealer_play(sess)
                outcome, amt_text = self.core.evaluate(sess)
                final_embed = self._build_embed(sess, reveal_dealer=True, author=ctx.author)
                final_embed.add_field(name="Kết quả", value=f"{outcome}\n{amt_text}", inline=False)
                view.disable_all_items()
                try:
                    await interaction.message.edit(embed=final_embed, view=view)
                except Exception:
                    pass
                self.manager.pop_session(owner_id)
                await interaction.response.send_message("Ván đã kết thúc.", ephemeral=True)

            # attach callbacks
            view.hit.callback = lambda inter, btn=None, fn=_on_hit: asyncio.create_task(fn(inter))
            view.stand.callback = lambda inter, btn=None, fn=_on_stand: asyncio.create_task(fn(inter))

            # send message
            try:
                msg = await ctx.reply(embed=embed, view=view, mention_author=False)
            except Exception:
                # fallback: send text
                await ctx.reply("Không thể gửi embed/view ở kênh này.", mention_author=False)
                return

        @commands.command(name="bjstop")
        async def bj_stop(self, ctx: commands.Context):
            """Dừng (hủy) ván hiện tại của người dùng"""
            uid = ctx.author.id
            sess = self.manager.get_session(uid)
            if not sess:
                return await ctx.reply("Bạn không có ván đang chơi.", mention_author=False)
            # finalize and show result
            self.core.finalize_session(sess)
            outcome, amt_text = self.core.evaluate(sess)
            final_embed = self._build_embed(sess, reveal_dealer=True, author=ctx.author)
            final_embed.add_field(name="Kết quả", value=f"{outcome}\n{amt_text}", inline=False)
            # try edit original message if we have message_id
            msg_id = sess.get("message_id")
            try:
                if msg_id:
                    msg = await ctx.channel.fetch_message(msg_id)
                    tmp_view = BJView(uid)
                    tmp_view.disable_all_items()
                    await msg.edit(embed=final_embed, view=tmp_view)
            except Exception:
                pass
            self.manager.pop_session(uid)
            await ctx.reply("Ván đã bị dừng và hiển thị kết quả.", mention_author=False)

        def _build_embed(self, session_obj, reveal_dealer: bool, author):
            """Tiện ích dựng embed từ session (có thể tuỳ chỉnh)"""
            e = Embed(title="Blackjack", color=0x5865F2)
            p_lbl = ", ".join([card_to_label(c) for c in session_obj.player])
            e.add_field(name="Bạn", value=f"{p_lbl}\n**Tổng:** {session_obj.player_value}", inline=False)
            if reveal_dealer:
                d_lbl = ", ".join([card_to_label(c) for c in session_obj.dealer])
                e.add_field(name="Dealer", value=f"{d_lbl}\n**Tổng:** {session_obj.dealer_value}", inline=False)
            else:
                first = session_obj.dealer[0]
                hidden_count = max(0, len(session_obj.dealer) - 1)
                hidden = ", ".join(["??"] * hidden_count) if hidden_count else ""
                e.add_field(name="Dealer", value=f"{card_to_label(first)}{', ' + hidden if hidden else ''}\n**Tổng:** ?", inline=False)
            e.set_footer(text=f"Cược: {session_obj.bet} — Người chơi: {author.display_name}")
            # store message_id later by caller
            return e

    # helper to add cog cleanly
    async def setup(bot):
        await bot.add_cog(BlackjackUI(bot))

# =========================
# CORE LOGIC
# =========================

@dataclass
class Card:
    rank: int  # 1..13 (A=1, J=11, Q=12, K=13)
    suit: str  # one of SUITS

    def to_dict(self) -> Dict[str, Any]:
        return {"rank": self.rank, "suit": self.suit}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Card":
        return Card(rank=int(d["rank"]), suit=str(d["suit"]))

@dataclass
class Session:
    owner_id: int
    bet: int
    deck: List[Card]
    player: List[Card]
    dealer: List[Card]
    player_value: int
    dealer_value: int
    started_at: int
    message_id: Optional[int] = None
    finished: bool = False

    def to_dict(self):
        return {
            "owner_id": self.owner_id,
            "bet": self.bet,
            "deck": [c.to_dict() for c in self.deck],
            "player": [c.to_dict() for c in self.player],
            "dealer": [c.to_dict() for c in self.dealer],
            "player_value": self.player_value,
            "dealer_value": self.dealer_value,
            "started_at": self.started_at,
            "message_id": self.message_id,
            "finished": self.finished,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Session":
        return Session(
            owner_id=int(d["owner_id"]),
            bet=int(d["bet"]),
            deck=[Card.from_dict(x) for x in d.get("deck", [])],
            player=[Card.from_dict(x) for x in d.get("player", [])],
            dealer=[Card.from_dict(x) for x in d.get("dealer", [])],
            player_value=int(d.get("player_value", 0)),
            dealer_value=int(d.get("dealer_value", 0)),
            started_at=int(d.get("started_at", int(time.time()))),
            message_id=d.get("message_id"),
            finished=bool(d.get("finished", False)),
        )

class SessionManager:
    """
    Quản lý session (mặc định in-memory).
    Bạn có thể subclass hoặc truyền object khác có cùng API:
      get_session(owner_id) -> Session|None
      set_session(owner_id, session)
      pop_session(owner_id) -> Optional[Session]
      all_sessions() -> Dict[int, Session]
    """
    def __init__(self):
        self._store: Dict[int, Session] = {}

    def get_session(self, owner_id: int) -> Optional[Session]:
        return self._store.get(int(owner_id))

    def set_session(self, owner_id: int, session: Session):
        self._store[int(owner_id)] = session

    def pop_session(self, owner_id: int) -> Optional[Session]:
        return self._store.pop(int(owner_id), None)

    def all_sessions(self) -> Dict[int, Session]:
        return dict(self._store)

class BlackjackCore:
    """
    Core thuần của Blackjack — mọi hàm trả/nhận cấu trúc Python thuần (Session, Card).
    Thiết kế mở: không tương tác Discord, không quản lý sessions global (dùng SessionManager).
    Luật:
      - Ranks: 1..13 (A=1, 2..10, J=11, Q=12, K=13)
      - A luôn = 1 (không hỗ trợ A=11)
      - >21 là thua
      - Dealer rút trong khi value < 17 (chuẩn)
    """
    SUITS = ["♠", "♥", "♦", "♣"]
    MAX_BET = 100_000

    def __init__(self):
        # có thể cấu hình sau
        self.max_attempts_init = 20

    # --- deck / card helpers ---
    def new_deck(self, shuffle: bool = True) -> List[Card]:
        deck = [Card(rank=r, suit=s) for s in self.SUITS for r in range(1, 14)]
        if shuffle:
            random.shuffle(deck)
        return deck

    def card_value(self, card: Card) -> int:
        return int(card.rank)

    def hand_value(self, hand: List[Card]) -> int:
        return sum(self.card_value(c) for c in hand)

    def card_to_label(self, card: Card) -> str:
        rank_lbl = {1: "A", 11: "J", 12: "Q", 13: "K"}.get(card.rank, str(card.rank))
        return f"{rank_lbl}{card.suit} ({card.rank})"

    # --- session lifecycle ---
    def create_session(self, owner_id: int, bet: int) -> Session:
        """Tạo session mới, đảm bảo player initial không vượt quá 21 (thử reshuffle vài lần)."""
        attempts = 0
        while True:
            attempts += 1
            deck = self.new_deck(shuffle=True)
            player = [deck.pop(), deck.pop()]
            dealer = [deck.pop(), deck.pop()]
            pv = self.hand_value(player)
            if pv <= 21 or attempts >= self.max_attempts_init:
                break
        session = Session(
            owner_id=int(owner_id),
            bet=int(bet),
            deck=deck,
            player=player,
            dealer=dealer,
            player_value=self.hand_value(player),
            dealer_value=self.hand_value(dealer),
            started_at=int(time.time()),
            message_id=None,
            finished=False
        )
        return session

    def draw_card(self, session: Session) -> Card:
        """Rút 1 lá (nếu deck hết thì reshuffle mới)."""
        if not session.deck:
            session.deck = self.new_deck(shuffle=True)
        return session.deck.pop()

    def player_hit(self, session: Session) -> Card:
        """Player bốc 1 lá, cập nhật player_value, trả về Card."""
        c = self.draw_card(session)
        session.player.append(c)
        session.player_value = self.hand_value(session.player)
        return c

    def dealer_play(self, session: Session):
        """Dealer rút theo luật: rút khi value < 17."""
        while True:
            dv = self.hand_value(session.dealer)
            if dv < 17:
                c = self.draw_card(session)
                session.dealer.append(c)
                continue
            break
        session.dealer_value = self.hand_value(session.dealer)

    def finalize_session(self, session: Session):
        """Cập nhật lại giá trị và đánh dấu finished."""
        session.player_value = self.hand_value(session.player)
        session.dealer_value = self.hand_value(session.dealer)
        session.finished = True

    def evaluate(self, session: Session) -> Tuple[str, str]:
        """
        So sánh kết quả; trả về (outcome_text, amount_text).
        amount_text chỉ mô tả +bet / -bet hoặc +0; core KHÔNG thay ví.
        """
        pv = session.player_value
        dv = session.dealer_value
        bet = session.bet
        if pv > 21:
            return (f"Bạn vượt quá 21 ({pv}) — Thua.", f"-{bet}")
        if dv > 21:
            return (f"Dealer vượt quá 21 ({dv}) — Bạn thắng!", f"+{bet}")
        if pv > dv:
            return (f"Bạn thắng! ({pv} vs {dv})", f"+{bet}")
        if pv < dv:
            return (f"Bạn thua. ({pv} vs {dv})", f"-{bet}")
        return (f"Hòa (push). ({pv} vs {dv})", "+0")

    # --- serialization helpers ---
    def session_to_json(self, session: Session) -> str:
        return json.dumps(session.to_dict(), ensure_ascii=False)

    def session_from_json(self, raw: str) -> Session:
        d = json.loads(raw)
        return Session.from_dict(d)

# --- helper functions for external use (convenience) ---
def card_to_label(card: Card) -> str:
    rank_lbl = {1: "A", 11: "J", 12: "Q", 13: "K"}.get(card.rank, str(card.rank))
    return f"{rank_lbl}{card.suit} ({card.rank})"

# =========================
# Lưu ý
# - Để dùng core trong project khác: from blackjack import BlackjackCore, SessionManager
# - Tạo Session: core.create_session(owner_id, bet)
# - Lưu session bằng SessionManager hoặc DB (serialize bằng session_to_json)
# - Khi player bốc: core.player_hit(session); nếu player_value>21 -> core.finalize_session(session); result=core.evaluate(session)
# - Khi player đứng: core.dealer_play(session); core.finalize_session(session); result=core.evaluate(session)
# - UI phần Discord đơn giản, cus lại nếu cần.
# =========================

