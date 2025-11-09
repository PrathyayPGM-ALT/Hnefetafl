# hnefatafl_multiplayer.py (with start menu + local pass-and-play)
import pygame
import sys
import socket
import threading
import json
import queue
import time


# ------------------ WINDOW / PYGAME ------------------
WIDTH = 900
HEIGHT = 900

pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT))
clock = pygame.time.Clock()
pygame.display.set_caption("Hnefatafl, NOT FALAFEL")

# ------------------ COLORS ------------------
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREEN = (0, 128, 0)
RED = (255, 0, 0)
GOLD = (255, 215, 0)
BROWN = (139, 69, 19)
GRAY = (60, 60, 60)
LIGHT_GRAY = (200, 200, 200)
YELLOW = (255, 255, 0)

# ------------------ GAME CONST ------------------
BOARD_SIZE = 9
CELL_SIZE = WIDTH // BOARD_SIZE
KING = 0
DEFENDER = 1
ATTACKER = 2

# ------------------ NETWORK CONFIG ------------------
SERVER_HOST = "100.76.152.128"
SERVER_PORT = 8765

# =====================================================
#                       NETWORK
# =====================================================
class NetClient:
    def __init__(self, host, port, room_code, nickname):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(3)  # ⏱️ 3 seconds max wait
        try:
            self.sock.connect((host, port))
        except socket.timeout:
            raise ConnectionError(f"Connection to {host}:{port} timed out.")
        except Exception as e:
            raise ConnectionError(f"Failed to connect: {e}")
        self.sock.settimeout(None)  # back to blocking for normal use
        self.sock_lock = threading.Lock()
        self.inbox = queue.Queue()
        self.alive = True

        # Join room with name
        self.send_json({"type": "join", "room": str(room_code), "name": nickname})

        # Listener thread
        self.t = threading.Thread(target=self._recv_loop, daemon=True)
        self.t.start()

    def send_json(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self.sock_lock:
            try:
                self.sock.sendall(data)
            except Exception:
                pass

    def _recv_loop(self):
        buff = b""
        while self.alive:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buff += chunk
                while b"\n" in buff:
                    line, buff = buff.split(b"\n", 1)
                    try:
                        obj = json.loads(line.decode("utf-8").strip())
                        self.inbox.put(obj)
                    except Exception:
                        pass
            except Exception:
                break
        self.alive = False

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass

# =====================================================
#                       GAME LOGIC
# =====================================================
class Hnefatafl:
    def __init__(self):
        self.board = [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
        self.selected_piece = None
        self.current_player = DEFENDER
        self.game_over = False
        self.winner = None
        self.setup_board()

        # Multiplayer / mode fields
        self.my_side = None        # "DEFENDER", "ATTACKER", or "LOCAL"
        self.turn_side = None      # "DEFENDER" or "ATTACKER"
        self.my_name = None
        self.opponent_name = None
        self.waiting = True        # lobby/wait state
        self.net = None

    def setup_board(self):
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                self.board[row][col] = None

        center = BOARD_SIZE // 2
        self.board[center][center] = KING

        # defenders around king
        self.board[center-1][center] = DEFENDER
        self.board[center+1][center] = DEFENDER
        self.board[center][center-1] = DEFENDER
        self.board[center][center+1] = DEFENDER
        self.board[center-1][center-1] = DEFENDER
        self.board[center-1][center+1] = DEFENDER
        self.board[center+1][center-1] = DEFENDER
        self.board[center+1][center+1] = DEFENDER

        edge_positions = [
            (0, center), (BOARD_SIZE-1, center),
            (center, 0), (center, BOARD_SIZE-1)
        ]
        for row, col in edge_positions:
            offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            for dr, dc in offsets:
                new_row, new_col = row + dr, col + dc
                if 0 <= new_row < BOARD_SIZE and 0 <= new_col < BOARD_SIZE:
                    if self.board[new_row][new_col] is None:
                        self.board[new_row][new_col] = ATTACKER

    def is_castle(self, row, col):
        return row == BOARD_SIZE // 2 and col == BOARD_SIZE // 2

    def is_throne(self, row, col):
        center = BOARD_SIZE // 2
        return (row == center and col == center) or \
               (row == center-1 and col == center) or \
               (row == center+1 and col == center) or \
               (row == center and col == center-1) or \
               (row == center and col == center+1)

    def is_edge(self, row, col):
        return row == 0 or row == BOARD_SIZE-1 or col == 0 or col == BOARD_SIZE-1

    def is_corner(self, row, col):
        return (row, col) in [(0,0), (0, BOARD_SIZE-1), (BOARD_SIZE-1, 0), (BOARD_SIZE-1, BOARD_SIZE-1)]

    def get_valid_moves(self, row, col):
        if self.board[row][col] is None:
            return []
        piece_type = self.board[row][col]
        valid_moves = []
        directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        for dr, dc in directions:
            for distance in range(1, BOARD_SIZE):
                new_row, new_col = row + dr * distance, col + dc * distance
                if not (0 <= new_row < BOARD_SIZE and 0 <= new_col < BOARD_SIZE):
                    break
                # attackers cannot move into corners (reserved)
                if piece_type == ATTACKER and self.is_corner(new_row, new_col):
                    continue
                # blocked by piece
                if self.board[new_row][new_col] is not None:
                    break
                # king can move onto edges (escape)
                if piece_type == KING and self.is_edge(new_row, new_col):
                    valid_moves.append((new_row, new_col))
                    continue
                valid_moves.append((new_row, new_col))
        return valid_moves

    def move_piece(self, from_row, from_col, to_row, to_col, send=True):
        if (to_row, to_col) not in self.get_valid_moves(from_row, from_col):
            return False

        piece_type = self.board[from_row][from_col]
        self.board[from_row][from_col] = None
        self.board[to_row][to_col] = piece_type

        self.check_captures(to_row, to_col)
        self.check_win_conditions()

        # Toggle numeric current_player for legacy UI compatibility
        self.current_player = DEFENDER if self.current_player == ATTACKER else ATTACKER

        # Toggle network/local side turn tracker
        if self.turn_side:
            self.turn_side = "DEFENDER" if self.turn_side == "ATTACKER" else "ATTACKER"

        if send and self.net:
            self.net.send_json({"type": "move",
                                "from": [from_row, from_col],
                                "to": [to_row, to_col]})
        return True

    def check_captures(self, row, col):
        moving_piece = self.board[row][col]
        directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        for dr, dc in directions:
            target_row, target_col = row + dr, col + dc
            if not (0 <= target_row < BOARD_SIZE and 0 <= target_col < BOARD_SIZE):
                continue
            target_piece = self.board[target_row][target_col]
            if target_piece is None or target_piece == moving_piece:
                continue
            opposite_row, opposite_col = target_row + dr, target_col + dc
            if not (0 <= opposite_row < BOARD_SIZE and 0 <= opposite_col < BOARD_SIZE):
                continue
            opposite_piece = self.board[opposite_row][opposite_col]
            if target_piece == KING:
                self.check_king_capture(target_row, target_col)
            elif opposite_piece == moving_piece:
                self.board[target_row][target_col] = None

    def check_king_capture(self, king_row, king_col):
        # In-castle: 4 attackers
        if self.is_castle(king_row, king_col):
            directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            attackers_count = 0
            for dr, dc in directions:
                adj_row, adj_col = king_row + dr, king_col + dc
                if (0 <= adj_row < BOARD_SIZE and 0 <= adj_col < BOARD_SIZE and 
                    self.board[adj_row][adj_col] == ATTACKER):
                    attackers_count += 1
            if attackers_count == 4:
                self.board[king_row][king_col] = None
                self.game_over = True
                self.winner = ATTACKER
        # Adjacent to castle: 3 attackers (castle counts if empty)
        elif self.is_throne(king_row, king_col):
            directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            attackers_count = 0
            for dr, dc in directions:
                adj_row, adj_col = king_row + dr, king_col + dc
                if (0 <= adj_row < BOARD_SIZE and 0 <= adj_col < BOARD_SIZE):
                    if self.board[adj_row][adj_col] == ATTACKER:
                        attackers_count += 1
                    elif self.is_castle(adj_row, adj_col):
                        attackers_count += 1
            if attackers_count >= 3:
                self.board[king_row][king_col] = None
                self.game_over = True
                self.winner = ATTACKER
        # Else: sandwiched by two attackers on opposite sides
        else:
            directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            for dr, dc in directions:
                adj_row, adj_col = king_row + dr, king_col + dc
                opp_row, opp_col = king_row - dr, king_col - dc
                if (0 <= adj_row < BOARD_SIZE and 0 <= adj_col < BOARD_SIZE and
                    0 <= opp_row < BOARD_SIZE and 0 <= opp_col < BOARD_SIZE):
                    if (self.board[adj_row][adj_col] == ATTACKER and 
                        self.board[opp_row][opp_col] == ATTACKER):
                        self.board[king_row][king_col] = None
                        self.game_over = True
                        self.winner = ATTACKER
                        break

    def check_win_conditions(self):
        # King escapes to an edge
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if self.board[row][col] == KING and self.is_edge(row, col):
                    self.game_over = True
                    self.winner = DEFENDER
                    return
        # King captured (handled elsewhere) -> confirm king presence
        king_exists = any(self.board[row][col] == KING 
                          for row in range(BOARD_SIZE) for col in range(BOARD_SIZE))
        if not king_exists:
            self.game_over = True
            self.winner = ATTACKER

# =====================================================
#                   RENDERING / UI
# =====================================================
def draw_board(game, status_msg=None):
    screen.fill(BROWN)

    # grid
    for row in range(BOARD_SIZE + 1):
        pygame.draw.line(screen, BLACK, (0, row * CELL_SIZE), (WIDTH, row * CELL_SIZE), 2)
        pygame.draw.line(screen, BLACK, (row * CELL_SIZE, 0), (row * CELL_SIZE, HEIGHT), 2)

    # castle + throne
    center = BOARD_SIZE // 2
    castle_rect = pygame.Rect(center * CELL_SIZE, center * CELL_SIZE, CELL_SIZE, CELL_SIZE)
    pygame.draw.rect(screen, (200, 200, 200), castle_rect)

    throne_positions = [(center-1, center), (center+1, center), (center, center-1), (center, center+1)]
    for r, c in throne_positions:
        throne_rect = pygame.Rect(c * CELL_SIZE, r * CELL_SIZE, CELL_SIZE, CELL_SIZE)
        pygame.draw.rect(screen, (220, 220, 220), throne_rect)

    # pieces
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            piece = game.board[row][col]
            cx = col * CELL_SIZE + CELL_SIZE // 2
            cy = row * CELL_SIZE + CELL_SIZE // 2
            radius = CELL_SIZE // 3
            if piece == KING:
                pygame.draw.circle(screen, GOLD, (cx, cy), radius)
                pygame.draw.circle(screen, BLACK, (cx, cy), radius, 2)
            elif piece == DEFENDER:
                pygame.draw.circle(screen, WHITE, (cx, cy), radius)
                pygame.draw.circle(screen, BLACK, (cx, cy), radius, 2)
            elif piece == ATTACKER:
                pygame.draw.circle(screen, RED, (cx, cy), radius)
                pygame.draw.circle(screen, BLACK, (cx, cy), radius, 2)

    # selected & valid moves
    if game.selected_piece:
        row, col = game.selected_piece
        highlight_rect = pygame.Rect(col * CELL_SIZE, row * CELL_SIZE, CELL_SIZE, CELL_SIZE)
        pygame.draw.rect(screen, YELLOW, highlight_rect, 3)
        for mr, mc in game.get_valid_moves(row, col):
            move_rect = pygame.Rect(mc * CELL_SIZE, mr * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(screen, (0, 255, 0), move_rect, 2)

    # status line
    font = pygame.font.Font(None, 36)
    line = None
    color = GREEN
    if game.game_over:
        winner_text = "Defenders Win!" if game.winner == DEFENDER else "Attackers Win!"
        line = winner_text
        color = RED
    else:
        if game.waiting:
            who = f" ({game.my_name})" if game.my_name else ""
            opp = f" vs {game.opponent_name}" if game.opponent_name else ""
            line = f"Waiting for opponent{who}{opp}..."
        else:
            if game.my_side == "LOCAL":
                turn = f"Turn: {game.turn_side}"
                line = f"Local Pass-and-Play | {turn}"
            else:
                turn = f"Turn: {game.turn_side}"
                mine = f"You are {game.my_side}"
                vs = f" vs {game.opponent_name}" if game.opponent_name else ""
                line = f"{turn} | {mine}{vs}"

    if status_msg:
        line = (line + " | " + status_msg) if line else status_msg

    if line:
        text = font.render(line, True, color)
        screen.blit(text, (WIDTH // 2 - text.get_width() // 2, 20))

def text_input_screen(prompt, digits_only=False, max_len=12):
    font = pygame.font.Font(None, 48)
    input_str = ""
    while True:
        screen.fill((45, 35, 25))
        txt = font.render(prompt, True, WHITE)
        screen.blit(txt, (50, 100))
        box = pygame.Rect(50, 180, 800, 60)
        pygame.draw.rect(screen, WHITE, box, 2)
        val = font.render(input_str, True, WHITE)
        screen.blit(val, (60, 190))
        hint = pygame.font.Font(None, 28).render("Enter to confirm, Esc to quit, Backspace to edit", True, (200,200,200))
        screen.blit(hint, (50, 260))
        pygame.display.flip()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    pygame.quit(); sys.exit()
                elif event.key == pygame.K_RETURN:
                    return input_str.strip()
                elif event.key == pygame.K_BACKSPACE:
                    input_str = input_str[:-1]
                else:
                    ch = event.unicode
                    if ch:
                        if len(input_str) < max_len:
                            if digits_only:
                                if ch.isdigit():
                                    input_str += ch
                            else:
                                # allow simple alnum + underscore/hyphen/space
                                if ch.isalnum() or ch in "_- ":
                                    input_str += ch
        clock.tick(30)

# ------------------ MENU HELPERS ------------------
def draw_button(rect, label, hovered=False):
    bg = (240, 200, 60) if hovered else (210, 180, 140)
    pygame.draw.rect(screen, bg, rect, border_radius=12)
    pygame.draw.rect(screen, BLACK, rect, 2, border_radius=12)
    font = pygame.font.SysFont(None, 48)
    text = font.render(label, True, BLACK)
    screen.blit(text, (rect.centerx - text.get_width()//2, rect.centery - text.get_height()//2))

def start_menu():
    """Return 'ONLINE' or 'LOCAL' based on the user's choice."""
    title_font = pygame.font.Font(None, 72)
    sub_font = pygame.font.Font(None, 32)

    # Layout
    btn_w, btn_h = 500, 80
    online_rect = pygame.Rect(WIDTH//2 - btn_w//2, HEIGHT//2 - 60, btn_w, btn_h)
    local_rect  = pygame.Rect(WIDTH//2 - btn_w//2, HEIGHT//2 + 40, btn_w, btn_h)

    while True:
        screen.fill((45, 35, 25))
        title = title_font.render("Hnefatafl", True, GOLD)
        subtitle = sub_font.render("Choose a mode", True, (220, 220, 220))
        screen.blit(title, (WIDTH//2 - title.get_width()//2, 140))
        screen.blit(subtitle, (WIDTH//2 - subtitle.get_width()//2, 210))

        mx, my = pygame.mouse.get_pos()
        draw_button(online_rect, "Multiplayer Online", online_rect.collidepoint(mx, my))
        draw_button(local_rect,  "Play With Friends (Local)", local_rect.collidepoint(mx, my))

        hint = sub_font.render("Press Esc to quit", True, (220,220,220))
        screen.blit(hint, (WIDTH//2 - hint.get_width()//2, HEIGHT - 80))

        pygame.display.flip()
        clock.tick(60)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                pygame.quit(); sys.exit()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if online_rect.collidepoint(event.pos):
                    return "ONLINE"
                if local_rect.collidepoint(event.pos):
                    return "LOCAL"
def show_message_screen(message):
    """Display a simple message while blocking operations run."""
    font = pygame.font.Font(None, 60)
    screen.fill((45, 35, 25))
    text = font.render(message, True, (255, 215, 0))
    screen.blit(text, (WIDTH//2 - text.get_width()//2, HEIGHT//2 - text.get_height()//2))
    pygame.display.flip()
    pygame.event.pump()   # Let OS process events

# =====================================================
#                       MAIN LOOP
# =====================================================
def main():
    # 0) Show start menu
    mode = start_menu()

    game = Hnefatafl()

    if mode == "ONLINE":
        # 1) Ask for room code and nickname
        room_code = text_input_screen("Enter room code (numbers only):", digits_only=True, max_len=4)
        nickname = text_input_screen("Enter your nickname:", digits_only=False, max_len=16)
        game.my_name = nickname

# Non-blocking threaded connection
        connected = [False]
        failed = [False]
        net_ref = [None]

        def connect_to_server():
            try:
                n = NetClient(SERVER_HOST, SERVER_PORT, room_code, nickname)
                net_ref[0] = n
                connected[0] = True
            except ConnectionError as e:
                show_message_screen(str(e))
                pygame.time.wait(2000)
                pygame.quit(); sys.exit()

        threading.Thread(target=connect_to_server, daemon=True).start()

        font = pygame.font.Font(None, 60)
        dots = ""
        start_time = time.time()

        while not (connected[0] or failed[0]):
            # simple animated dots
            dots = "." * ((int(time.time() - start_time) % 3) + 1)
            screen.fill((45, 35, 25))
            text = font.render(f"Connecting to server{dots}", True, (255, 215, 0))
            screen.blit(text, (WIDTH // 2 - text.get_width() // 2, HEIGHT // 2 - text.get_height() // 2))
            pygame.display.flip()
            pygame.event.pump()
            clock.tick(30)

        if failed[0]:
            screen.fill((80, 80, 80))
            fail_text = font.render("Connection failed. Press any key to exit.", True, (255, 150, 150))
            screen.blit(fail_text, (WIDTH // 2 - fail_text.get_width() // 2, HEIGHT // 2 - fail_text.get_height() // 2))
            pygame.display.flip()
            waiting = True
            while waiting:
                for event in pygame.event.get():
                    if event.type in (pygame.QUIT, pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
                        pygame.quit(); sys.exit()
                clock.tick(10)
        else:
            game.net = net_ref[0]



    else:
        # Local pass-and-play setup
        game.my_side = "LOCAL"
        game.turn_side = "DEFENDER"     # Defenders (incl. King) go first
        game.current_player = DEFENDER
        game.my_name = "You"
        game.opponent_name = "Friend"
        game.waiting = False
        status_msg = "Local match: Defenders start"

    # 3) Main loop
    while True:
        # Handle inbound network messages (ONLINE only)
        if game.net:
            try:
                while True:
                    msg = game.net.inbox.get_nowait()
                    mtype = msg.get("type")
                    if mtype == "waiting":
                        players = msg.get("players", [])
                        status_msg = "Waiting for opponent... (" + ", ".join(players) + ")"
                        game.waiting = True
                    elif mtype == "joined":
                        jn = msg.get("name","Someone")
                        status_msg = f"{jn} joined. Waiting for opponent..."
                        game.waiting = True
                    elif mtype == "start":
                        game.my_side = msg.get("your_side")
                        game.turn_side = msg.get("current_player")
                        game.current_player = ATTACKER if game.turn_side == "ATTACKER" else DEFENDER
                        game.opponent_name = msg.get("opponent_name","Opponent")
                        status_msg = f"You are {game.my_side}. Opponent: {game.opponent_name}"
                        game.waiting = False
                    elif mtype == "move":
                        fr = msg.get("from", [0,0]); to = msg.get("to", [0,0])
                        game.move_piece(fr[0], fr[1], to[0], to[1], send=False)
                    elif mtype == "opponent_left":
                        left_name = msg.get("name","Opponent")
                        status_msg = f"{left_name} left. Waiting for opponent..."
                        game.waiting = True
                        game.opponent_name = None
                    elif mtype == "error":
                        status_msg = f"Error: {msg.get('msg','')}"
                    elif mtype == "full":
                        status_msg = "Room is full"
            except queue.Empty:
                pass

        # Handle local events
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                if game.net: game.net.close()
                pygame.quit(); sys.exit()

            if not game.game_over and not game.waiting and event.type == pygame.MOUSEBUTTONDOWN:
                # Determine if input is allowed this click
                local_mode = (game.my_side == "LOCAL")
                if game.net:
                    my_turn = (
                        (game.my_side == "DEFENDER" and game.turn_side == "DEFENDER") or
                        (game.my_side == "ATTACKER" and game.turn_side == "ATTACKER")
                    )
                else:
                    # Local: always allow a click, but restrict by turn when selecting a piece
                    my_turn = True

                if not my_turn:
                    continue

                col = event.pos[0] // CELL_SIZE
                row = event.pos[1] // CELL_SIZE
                if 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE:
                    def belongs_to_turn(piece):
                        if piece is None:
                            return False
                        if game.turn_side == "DEFENDER":
                            return piece == DEFENDER or piece == KING
                        else:
                            return piece == ATTACKER

                    def belongs_to_me(piece):
                        # Online ownership
                        if game.my_side == "DEFENDER":
                            return piece == DEFENDER or piece == KING
                        else:
                            return piece == ATTACKER

                    if game.selected_piece:
                        from_row, from_col = game.selected_piece
                        piece = game.board[from_row][from_col]

                        # Can we try this move?
                        if local_mode:
                            allowed = belongs_to_turn(piece)
                        else:
                            allowed = belongs_to_me(piece)

                        if allowed and game.move_piece(from_row, from_col, row, col, send=(game.net is not None)):
                            game.selected_piece = None
                        else:
                            # Maybe select a different piece
                            piece2 = game.board[row][col]
                            if local_mode:
                                game.selected_piece = (row, col) if belongs_to_turn(piece2) else None
                            else:
                                game.selected_piece = (row, col) if piece2 is not None and belongs_to_me(piece2) else None
                    else:
                        piece = game.board[row][col]
                        if local_mode:
                            game.selected_piece = (row, col) if belongs_to_turn(piece) else None
                        else:
                            game.selected_piece = (row, col) if piece is not None and belongs_to_me(piece) else None

        draw_board(game, status_msg)
        pygame.display.flip()
        clock.tick(60)

if __name__ == "__main__":
    main()
