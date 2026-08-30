"""
SAM Simulator - APN Guidance + Threat Allocation + Pause/Speed Control
=======================================================================
Controls:
  T           - Spawn a random target (auto-allocates)
  A           - Force allocate missiles to all threats
  F / SPACE   - Manual fire at highest-threat target
  P           - Pause / Resume
  +  / =      - Speed Up  (max ×8)
  -           - Slow Down (min ×0.125)
  0           - Reset speed to ×1
  R           - Reset simulation
  ESC         - Quit
"""

import pygame
import math
import random
import sys
from typing import List, Optional, Dict

# ─────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────
WIDTH, HEIGHT   = 1200, 800
FPS             = 60
BACKGROUND      = (5, 8, 12)

LAUNCHER_X      = WIDTH  // 2
LAUNCHER_Y      = HEIGHT - 60
LAUNCHER_COLOR  = (255, 165, 0)

# Target
TARGET_BASE_SPEED   = 3.5
TARGET_NOISE_VEL    = 0.12
TARGET_NOISE_HEAD   = 0.8

# Missile
MISSILE_SPEED       = 10.0
MISSILE_NAV_CONST   = 4.5
MISSILE_MAX_TURN    = 6.0
MISSILE_TRAIL_LEN   = 90

# APN tuning
APN_LOS_FILTER_ALPHA = 0.35
APN_ACCEL_FILTER     = 0.25
ZEM_SCALE            = 0.018

# Hit
HIT_RADIUS          = 14

# Explosion
PARTICLE_COUNT      = 35
PARTICLE_LIFESPAN   = 45

# Allocation
MAX_MISSILES_PER_TARGET = 2
SALVO_DELAY_FRAMES      = 18
THREAT_W_CLOSING        = 2.0
THREAT_W_DISTANCE       = 1.0

# Speed control
SPEED_STEPS   = [0.05, 0.125, 0.25, 0.5, 1.0, 2.0]
SPEED_DEFAULT = 3   # index into SPEED_STEPS  -> x1.0

# ─────────────────────────────────────────────────────────────────
#  COLOURS
# ─────────────────────────────────────────────────────────────────
COL_MISSILE          = (255, 220, 60)
COL_MISSILE_2        = (80,  220, 255)
COL_TARGET           = (220, 80,  80)
COL_TARGET_BORDER    = (255, 140, 140)
COL_HUD              = (100, 255, 100)
COL_HUD_DIM          = (55,  160, 55)
COL_PARAM_BRIGHT     = (230, 238, 245)   # near-white for entity params
COL_PARAM_DIM        = (150, 165, 178)   # muted silver
COL_PARAM_LABEL      = (180, 215, 255)   # light-blue label
COL_TITLE            = (60,  255, 60)
COL_SUBTITLE         = (60,  200, 60)
COL_EXPLOSION        = [(255,255,150),(255,200,80),(255,120,30),(200,60,20),(120,30,10)]
COL_SPEED_SLOW       = (60,  200, 255)
COL_SPEED_FAST       = (255, 120, 40)
COL_SPEED_NORMAL     = (100, 255, 100)
COL_PAUSE_TEXT       = (255, 240, 60)

# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────

def heading_to_vec(deg: float) -> pygame.Vector2:
    """0=up, CW convention to unit Vector2."""
    rad = math.radians(deg - 90)
    return pygame.Vector2(math.cos(rad), math.sin(rad))


def vec_to_heading(v: pygame.Vector2) -> float:
    if v.length_squared() < 1e-9:
        return 0.0
    return (math.degrees(math.atan2(v.y, v.x)) + 90) % 360

# ─────────────────────────────────────────────────────────────────
#  TERRAIN
# ─────────────────────────────────────────────────────────────────

def generate_terrain(width: int, height: int, y_base: int) -> List[tuple]:
    pts = [(0, height)]
    x = 0
    while x <= width:
        pts.append((x, y_base + random.randint(-40, 15)))
        x += random.randint(30, 80)
    pts.append((width, height))
    return pts

# ─────────────────────────────────────────────────────────────────
#  PARTICLE
# ─────────────────────────────────────────────────────────────────

class Particle:
    def __init__(self, pos: pygame.Vector2, palette: List[tuple]):
        self.pos      = pygame.Vector2(pos)
        self.vel      = heading_to_vec(random.uniform(0, 360)) * random.uniform(1.5, 6.0)
        self.life     = PARTICLE_LIFESPAN
        self.max_life = PARTICLE_LIFESPAN
        self.color    = random.choice(palette)
        self.size     = random.randint(2, 5)

    def update(self):
        self.pos  += self.vel
        self.vel  *= 0.93
        self.life -= 1

    def draw(self, surf: pygame.Surface):
        frac = self.life / self.max_life
        r, g, b = self.color
        pygame.draw.circle(surf, (int(r*frac), int(g*frac), int(b*frac)),
                           (int(self.pos.x), int(self.pos.y)), self.size)

    @property
    def dead(self) -> bool:
        return self.life <= 0

# ─────────────────────────────────────────────────────────────────
#  TARGET
# ─────────────────────────────────────────────────────────────────

class Target:
    RADIUS = 9
    _id_counter = 0

    def __init__(self):
        Target._id_counter += 1
        self.id    = Target._id_counter
        self.label = f"TGT-{self.id:02d}"
        self._spawn()

    def _spawn(self):
        """All targets spawn at edges and fly DOWNWARD / cross-screen."""
        side = random.choice(["top", "left", "right"])
        if side == "top":
            self.pos     = pygame.Vector2(random.randint(80, WIDTH - 80), -20)
            self.heading = random.uniform(135, 225)
        elif side == "left":
            self.pos     = pygame.Vector2(-20, random.randint(30, HEIGHT // 2))
            self.heading = random.uniform(50, 155)
        else:
            self.pos     = pygame.Vector2(WIDTH + 20, random.randint(30, HEIGHT // 2))
            self.heading = random.uniform(205, 310)

        self.speed             = TARGET_BASE_SPEED + random.uniform(-0.5, 1.5)
        self.vel               = heading_to_vec(self.heading) * self.speed
        self.alive             = True
        self.trail: List[pygame.Vector2] = []
        self.missiles_assigned = 0

    def update(self, dt: float):
        noisy_speed    = self.speed + random.gauss(0, TARGET_NOISE_VEL)
        self.heading  += random.gauss(0, TARGET_NOISE_HEAD)
        self.heading  %= 360
        self.vel       = heading_to_vec(self.heading) * noisy_speed
        self.trail.append(pygame.Vector2(self.pos))
        if len(self.trail) > 55:
            self.trail.pop(0)
        self.pos += self.vel
        margin = 80
        if (self.pos.x < -margin or self.pos.x > WIDTH + margin or
                self.pos.y < -margin or self.pos.y > HEIGHT + margin):
            self.alive = False

    def draw(self, surf: pygame.Surface, font_param, font_label):
        if not self.alive:
            return

        # Trail
        for i, pt in enumerate(self.trail):
            a = int(85 * i / max(len(self.trail), 1))
            pygame.draw.circle(surf, (a, a, a), (int(pt.x), int(pt.y)), 2)

        ix, iy = int(self.pos.x), int(self.pos.y)

        # Engagement ring
        if self.missiles_assigned > 0:
            ring_col = (255, 55, 55) if self.missiles_assigned >= MAX_MISSILES_PER_TARGET \
                       else (255, 185, 30)
            pygame.draw.circle(surf, ring_col, (ix, iy), self.RADIUS + 6, 2)

        # Body
        pygame.draw.circle(surf, COL_TARGET,        (ix, iy), self.RADIUS)
        pygame.draw.circle(surf, COL_TARGET_BORDER, (ix, iy), self.RADIUS, 2)

        # Heading tick
        tip = self.pos + heading_to_vec(self.heading) * (self.RADIUS + 8)
        pygame.draw.line(surf, (255, 80, 80), (ix, iy), (int(tip.x), int(tip.y)), 2)

        # Allocation badge above circle
        if self.missiles_assigned > 0:
            badge_col = (255, 70, 70) if self.missiles_assigned >= MAX_MISSILES_PER_TARGET \
                        else (255, 210, 50)
            bsurf = font_label.render(f"x{self.missiles_assigned}", True, badge_col)
            surf.blit(bsurf, (ix - bsurf.get_width()//2, iy - self.RADIUS - 20))

        # ── Parameter HUD panel ────────────────────────────────────
        spd_str = f"V {self.speed:5.2f} ({self.speed*0.96:.2f})"
        hdg_str = f"H {self.heading - 180:+7.2f}"
        lbl_str = self.label

        ox, oy  = ix + 15, iy - 42
        line_h  = 18

        # Calculate panel width
        max_w = max(
            font_label.size(lbl_str)[0],
            font_param.size(spd_str)[0],
            font_param.size(hdg_str)[0],
        ) + 12

        # Semi-transparent background panel
        panel_h = line_h * 3 + 6
        panel   = pygame.Surface((max_w, panel_h), pygame.SRCALPHA)
        panel.fill((5, 10, 18, 195))
        surf.blit(panel, (ox - 5, oy - 3))

        # Left accent bar (coloured strip)
        pygame.draw.rect(surf, COL_TARGET_BORDER, (ox - 5, oy - 3, 2, panel_h))

        # Label row
        surf.blit(font_label.render(lbl_str, True, COL_PARAM_LABEL), (ox, oy))
        oy += line_h - 1
        # Speed row
        surf.blit(font_param.render(spd_str, True, COL_PARAM_BRIGHT), (ox, oy))
        oy += line_h
        # Heading row
        surf.blit(font_param.render(hdg_str, True, COL_PARAM_DIM), (ox, oy))

# ─────────────────────────────────────────────────────────────────
#  MISSILE  (APN guidance)
# ─────────────────────────────────────────────────────────────────

class Missile:
    """
    Augmented Proportional Navigation (APN) interceptor.

    Command = PN_term + APN_accel_feedforward + ZEM_endgame
      PN   : N x Vc x filtered_LOS_rate
      APN  : (N/2) x a_T_perp   – compensates target maneuver early
      ZEM  : cross_miss / t_go  – sharpens final approach phase
    """
    LENGTH = 15
    WIDTH  = 4

    def __init__(self, pos: pygame.Vector2, target: "Target", salvo_index: int = 0):
        self.pos         = pygame.Vector2(pos)
        self.target      = target
        self.salvo_index = salvo_index
        self.speed       = MISSILE_SPEED
        self.heading     = vec_to_heading(target.pos - pos)
        self.vel         = heading_to_vec(self.heading) * self.speed
        self.alive       = True
        self.trail: List[pygame.Vector2] = []

        self._prev_los_rad: Optional[float]        = None
        self._los_rate_filt: float                  = 0.0
        self._prev_target_vel: Optional[pygame.Vector2] = None
        self._target_accel_filt                     = pygame.Vector2(0, 0)

    # ── APN core ─────────────────────────────────────────────────

    def _apn_guidance(self) -> float:
        r_vec = self.target.pos - self.pos
        r_mag = r_vec.length()
        if r_mag < 1e-3:
            return 0.0

        los_now = math.atan2(r_vec.y, r_vec.x)

        if self._prev_los_rad is None:
            self._prev_los_rad    = los_now
            self._prev_target_vel = pygame.Vector2(self.target.vel)
            return 0.0

        # Raw LOS rate (rad/frame), wrapped to [-pi, pi]
        los_rate_raw = los_now - self._prev_los_rad
        if los_rate_raw >  math.pi: los_rate_raw -= 2 * math.pi
        if los_rate_raw < -math.pi: los_rate_raw += 2 * math.pi
        self._prev_los_rad = los_now

        # Exponential low-pass filter – smooths seeker noise
        a = APN_LOS_FILTER_ALPHA
        self._los_rate_filt = a * los_rate_raw + (1 - a) * self._los_rate_filt

        # Closing velocity Vc (positive = approaching)
        r_hat = r_vec / r_mag
        v_rel = self.target.vel - self.vel
        vc    = -r_hat.dot(v_rel)

        # Estimate target lateral acceleration via finite difference + EMA
        cur_vel = pygame.Vector2(self.target.vel)
        if self._prev_target_vel is not None:
            b = APN_ACCEL_FILTER
            self._target_accel_filt = (b * (cur_vel - self._prev_target_vel)
                                       + (1 - b) * self._target_accel_filt)
        self._prev_target_vel = cur_vel

        los_perp = pygame.Vector2(-r_hat.y, r_hat.x)
        a_t_perp = self._target_accel_filt.dot(los_perp)

        N        = MISSILE_NAV_CONST
        pn_term  = N * max(vc, 0.1) * math.degrees(self._los_rate_filt) * 0.06
        apn_term = (N / 2.0) * a_t_perp * 0.9

        t_go     = r_mag / max(abs(vc), 0.5)
        zem      = r_hat.x * v_rel.y - r_hat.y * v_rel.x
        zem_term = zem * ZEM_SCALE / max(t_go * 0.016, 0.05)

        return pn_term + apn_term + zem_term

    # ── update ───────────────────────────────────────────────────

    def update(self, dt: float):
        if not self.target.alive:
            self.alive = False
            return
        turn = self._apn_guidance()
        turn = max(-MISSILE_MAX_TURN, min(MISSILE_MAX_TURN, turn))
        self.heading  = (self.heading + turn) % 360
        self.vel      = heading_to_vec(self.heading) * self.speed
        self.trail.append(pygame.Vector2(self.pos))
        if len(self.trail) > MISSILE_TRAIL_LEN:
            self.trail.pop(0)
        self.pos += self.vel
        if (self.pos.x < -50 or self.pos.x > WIDTH + 50 or
                self.pos.y < -50 or self.pos.y > HEIGHT + 50):
            self.alive = False

    # ── draw ─────────────────────────────────────────────────────

    def draw(self, surf: pygame.Surface, font_param, font_label):
        if not self.alive:
            return

        # Smoke trail
        n = len(self.trail)
        for i, pt in enumerate(self.trail):
            frac   = i / max(n, 1)
            bright = int(30 + 130 * frac)
            rad    = max(1, int(3 * (1 - frac) + 1))
            pygame.draw.circle(surf, (bright, bright, bright), (int(pt.x), int(pt.y)), rad)

        # Triangle body
        body_col  = COL_MISSILE_2 if self.salvo_index == 1 else COL_MISSILE
        angle_rad = math.radians(self.heading - 90)
        ca, sa    = math.cos(angle_rad), math.sin(angle_rad)

        def rot(lx, ly):
            return (int(lx*ca - ly*sa + self.pos.x),
                    int(lx*sa + ly*ca + self.pos.y))

        nose  = rot(0,  -self.LENGTH)
        left  = rot(-self.WIDTH,  self.LENGTH // 2)
        right = rot( self.WIDTH,  self.LENGTH // 2)
        tail  = rot(0,   self.LENGTH // 3)
        pygame.draw.polygon(surf, body_col,       [nose, left, tail, right])
        pygame.draw.polygon(surf, (255, 255, 200), [nose, left, tail, right], 1)

        # Engine glow
        glow_col = (80, 200, 255) if self.salvo_index == 1 else (255, 100, 0)
        pygame.draw.circle(surf, glow_col, rot(0, self.LENGTH // 2), 3)

        # ── Parameter HUD panel ────────────────────────────────────
        tag_str = f"[S{self.salvo_index + 1}]"
        spd_str = f"V {self.speed:5.2f} ({self.speed*0.96:.2f})"
        hdg_str = f"H {(self.heading - 180):+7.2f}"

        ix, iy  = int(self.pos.x), int(self.pos.y)
        ox, oy  = ix + 17, iy - 42
        line_h  = 18

        max_w = max(
            font_label.size(tag_str)[0],
            font_param.size(spd_str)[0],
            font_param.size(hdg_str)[0],
        ) + 12

        panel_h = line_h * 3 + 6
        panel   = pygame.Surface((max_w, panel_h), pygame.SRCALPHA)
        panel.fill((5, 10, 18, 195))
        surf.blit(panel, (ox - 5, oy - 3))

        # Left accent bar (missile colour)
        pygame.draw.rect(surf, body_col, (ox - 5, oy - 3, 2, panel_h))

        surf.blit(font_label.render(tag_str, True, body_col),       (ox, oy))
        oy += line_h - 1
        surf.blit(font_param.render(spd_str, True, COL_PARAM_BRIGHT), (ox, oy))
        oy += line_h
        surf.blit(font_param.render(hdg_str, True, COL_PARAM_DIM),    (ox, oy))

# ─────────────────────────────────────────────────────────────────
#  MISSILE ALLOCATOR
# ─────────────────────────────────────────────────────────────────

class MissileAllocator:
    """
    Threat-priority fire-control system.
    Score = W_closing x Vc  +  W_distance x (1000 / dist)
    Fires up to MAX_MISSILES_PER_TARGET per target,
    staggered by SALVO_DELAY_FRAMES for best intercept geometry.
    """

    def __init__(self):
        self._queue: List[tuple] = []
        self._frame: int = 0

    def threat_score(self, target: Target, launcher_pos: pygame.Vector2) -> float:
        r_vec = launcher_pos - target.pos
        dist  = max(r_vec.length(), 1.0)
        r_hat = r_vec / dist
        vc    = max(target.vel.dot(r_hat), 0.0)
        return THREAT_W_CLOSING * vc + THREAT_W_DISTANCE * (1000.0 / dist)

    def allocate(self, targets: List[Target], missiles: List[Missile],
                 launcher_pos: pygame.Vector2):
        live = [t for t in targets if t.alive]
        if not live:
            return
        for t in live:
            t.missiles_assigned = sum(1 for m in missiles if m.alive and m.target is t)
        ranked         = sorted(live, key=lambda t: self.threat_score(t, launcher_pos),
                                reverse=True)
        queued_targets = {e[0] for e in self._queue}
        for target in ranked:
            if target in queued_targets:
                continue
            needed = MAX_MISSILES_PER_TARGET - target.missiles_assigned
            for i in range(needed):
                salvo_idx  = target.missiles_assigned + i
                fire_frame = self._frame + i * SALVO_DELAY_FRAMES
                self._queue.append((target, salvo_idx, fire_frame))

    def tick(self, targets: List[Target], missiles: List[Missile],
             launcher_pos: pygame.Vector2) -> List[Missile]:
        self._frame += 1
        new_missiles: List[Missile] = []
        pending = []
        for (target, salvo_idx, fire_frame) in self._queue:
            if not target.alive:
                continue
            if self._frame >= fire_frame:
                m = Missile(launcher_pos, target, salvo_index=salvo_idx)
                target.missiles_assigned += 1
                new_missiles.append(m)
            else:
                pending.append((target, salvo_idx, fire_frame))
        self._queue = pending
        return new_missiles

    def reset(self):
        self._queue.clear()
        self._frame = 0

# ─────────────────────────────────────────────────────────────────
#  LAUNCHER DRAW
# ─────────────────────────────────────────────────────────────────

def draw_launcher(surf: pygame.Surface, font):
    x, y = LAUNCHER_X, LAUNCHER_Y
    pygame.draw.rect(surf, (80, 80, 80), (x - 32, y + 10, 64, 14), border_radius=4)
    pygame.draw.rect(surf, LAUNCHER_COLOR, (x - 4, y - 18, 8, 28), border_radius=3)
    if random.random() < 0.3:
        pygame.draw.circle(surf, (255, 160, 30), (x, y - 18), 4)
    lbl = font.render("LAUNCHER", True, (180, 180, 180))
    surf.blit(lbl, (x - lbl.get_width() // 2, y + 28))

# ─────────────────────────────────────────────────────────────────
#  SIMULATION
# ─────────────────────────────────────────────────────────────────

class Simulation:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        pygame.display.set_caption("SAM Simulator - APN + Allocation")
        self.clock  = pygame.time.Clock()

        # ── Fonts ──────────────────────────────────────────────────
        self.font_title  = pygame.font.Font(None, 28)
        self.font_sub    = pygame.font.Font(None, 18)
        self.font_hud    = pygame.font.Font(None, 14)
        # Larger, bolder parameter fonts for entity HUD readouts
        self.font_param  = pygame.font.Font(None, 13)  # V / H values
        self.font_label  = pygame.font.Font(None, 12)  # entity label/tag
        self.font_small  = pygame.font.Font(None, 11)             # misc small text

        self.terrain   = generate_terrain(WIDTH, HEIGHT, HEIGHT - 80)
        self.allocator = MissileAllocator()

        # ── Speed / Pause state ────────────────────────────────────
        self.speed_idx   = SPEED_DEFAULT
        self.paused      = False
        self._pause_tick = 0

        self.reset()

    # ── Properties ───────────────────────────────────────────────

    @property
    def game_speed(self) -> float:
        return SPEED_STEPS[self.speed_idx]

    # ── Reset ─────────────────────────────────────────────────────

    def reset(self):
        self.targets:   List[Target]   = []
        self.missiles:  List[Missile]  = []
        self.particles: List[Particle] = []
        self.kills      = 0
        self.allocator.reset()
        Target._id_counter = 0

    # ── Spawn / Fire ──────────────────────────────────────────────

    def spawn_target(self):
        t = Target()
        self.targets.append(t)
        self.allocator.allocate(self.targets, self.missiles,
                                pygame.Vector2(LAUNCHER_X, LAUNCHER_Y))

    def manual_launch(self):
        lp   = pygame.Vector2(LAUNCHER_X, LAUNCHER_Y)
        live = [t for t in self.targets if t.alive]
        if not live:
            return
        for t in live:
            t.missiles_assigned = sum(1 for m in self.missiles if m.alive and m.target is t)
        cands = [t for t in live if t.missiles_assigned < MAX_MISSILES_PER_TARGET]
        if not cands:
            return
        best = max(cands, key=lambda t: self.allocator.threat_score(t, lp))
        m    = Missile(lp, best, salvo_index=best.missiles_assigned)
        best.missiles_assigned += 1
        self.missiles.append(m)

    # ── Collision ─────────────────────────────────────────────────

    def check_hits(self):
        for missile in self.missiles:
            if not missile.alive:
                continue
            for target in self.targets:
                if not target.alive:
                    continue
                if (missile.pos - target.pos).length() < HIT_RADIUS:
                    mid = (missile.pos + target.pos) / 2
                    for _ in range(PARTICLE_COUNT):
                        self.particles.append(Particle(mid, COL_EXPLOSION))
                    # Cancel all missiles targeting the same object
                    for m2 in self.missiles:
                        if m2.alive and m2.target is target:
                            m2.alive = False
                    target.alive             = False
                    target.missiles_assigned = 0
                    self.kills              += 1

    # ── Engagement Lines ──────────────────────────────────────────

    def draw_engagement_lines(self):
        engage_map: Dict = {}
        for m in self.missiles:
            if m.alive and m.target.alive:
                engage_map.setdefault(m.target, []).append(m)
        for target, mlist in engage_map.items():
            tx, ty = int(target.pos.x), int(target.pos.y)
            for m in mlist:
                col  = (80, 200, 255) if m.salvo_index == 1 else (60, 180, 255)
                dx, dy = tx - LAUNCHER_X, ty - LAUNCHER_Y
                length = math.hypot(dx, dy)
                if length < 1:
                    continue
                steps = int(length / 14)
                for s in range(steps):
                    if s % 2 == 0:
                        x0 = int(LAUNCHER_X + dx * s       / max(steps, 1))
                        y0 = int(LAUNCHER_Y + dy * s       / max(steps, 1))
                        x1 = int(LAUNCHER_X + dx * (s + 1) / max(steps, 1))
                        y1 = int(LAUNCHER_Y + dy * (s + 1) / max(steps, 1))
                        pygame.draw.line(self.screen, col, (x0, y0), (x1, y1), 1)

    # ── Speed / Pause Panel ───────────────────────────────────────

    def draw_speed_panel(self):
        """Top-right: pause badge + speed bar."""
        s = self.screen
        spd = self.game_speed
        px, py = WIDTH - 224, 12

        # Pause badge (blinks)
        if self.paused:
            self._pause_tick = (self._pause_tick + 1) % 60
            if self._pause_tick < 40:
                bg = pygame.Surface((96, 28), pygame.SRCALPHA)
                bg.fill((190, 15, 15, 210))
                s.blit(bg, (px, py))
                pt = self.font_hud.render("II  PAUSED", True, COL_PAUSE_TEXT)
                s.blit(pt, (px + 6, py + 5))
        else:
            self._pause_tick = 0

        # Speed bar
        bx, by = px, py + 36
        if   spd < 1.0: spd_col = COL_SPEED_SLOW
        elif spd > 1.0: spd_col = COL_SPEED_FAST
        else:           spd_col = COL_SPEED_NORMAL

        # Track
        track = pygame.Surface((210, 16), pygame.SRCALPHA)
        track.fill((18, 18, 18, 185))
        s.blit(track, (bx, by))
        # Fill
        fill_w = int(210 * (self.speed_idx + 1) / len(SPEED_STEPS))
        if fill_w > 0:
            fill = pygame.Surface((fill_w, 16), pygame.SRCALPHA)
            fill.fill((*spd_col, 165))
            s.blit(fill, (bx, by))
        # Border + ticks
        pygame.draw.rect(s, spd_col, (bx, by, 210, 16), 1)
        for i in range(len(SPEED_STEPS)):
            tx  = bx + int(210 * (i + 1) / len(SPEED_STEPS))
            tc  = spd_col if i <= self.speed_idx else (45, 45, 45)
            pygame.draw.line(s, tc, (tx, by), (tx, by + 16), 1)

        # Speed label
        lbl = self.font_hud.render(f"SPEED  x{spd:.3g}", True, spd_col)
        s.blit(lbl, (bx, by + 20))
        # Key hints
        hint = self.font_small.render("[-] slow  [+] fast  [0] x1  [P] pause",
                                      True, COL_HUD_DIM)
        s.blit(hint, (bx, by + 38))

    # ── Main HUD ─────────────────────────────────────────────────

    def draw_hud(self):
        s = self.screen

        # Title
        title = self.font_title.render("SAM Simulator", True, COL_TITLE)
        s.blit(title, (WIDTH // 2 - title.get_width() // 2, 14))
        sub1 = self.font_sub.render("Designed and Developed by Time Lapse Coder", True, COL_SUBTITLE)
        sub2 = self.font_sub.render("APN Guidance  |  Threat-Priority Allocation", True, COL_SUBTITLE)
        s.blit(sub1, (WIDTH // 2 - sub1.get_width() // 2, 50))
        s.blit(sub2, (WIDTH // 2 - sub2.get_width() // 2, 70))

        # ── Stats (bottom-left) ───────────────────────────────────
        active_t = sum(1 for t in self.targets  if t.alive)
        active_m = sum(1 for m in self.missiles if m.alive)
        stats = [
            f"Targets  : {active_t}",
            f"Missiles : {active_m}",
            f"Kills    : {self.kills}",
            f"LauncherX: {LAUNCHER_X}",
        ]
        for i, line in enumerate(stats):
            s.blit(self.font_hud.render(line, True, COL_HUD), (12, HEIGHT - 162 + i * 20))

        # ── Engagement table (bottom-right) ───────────────────────
        lp = pygame.Vector2(LAUNCHER_X, LAUNCHER_Y)
        live = sorted([t for t in self.targets if t.alive],
                      key=lambda t: self.allocator.threat_score(t, lp), reverse=True)
        hdr = self.font_hud.render("ENGAGEMENT TABLE", True, COL_TITLE)
        s.blit(hdr, (WIDTH - 275, HEIGHT - 170))
        chdr = self.font_small.render(f"{'Target':<10} {'Score':>7}  Msls", True, COL_HUD_DIM)
        s.blit(chdr, (WIDTH - 275, HEIGHT - 152))
        pygame.draw.line(s, COL_HUD_DIM,
                         (WIDTH - 275, HEIGHT - 138), (WIDTH - 22, HEIGHT - 138), 1)
        for i, t in enumerate(live[:6]):
            score = self.allocator.threat_score(t, lp)
            bar   = "+" * t.missiles_assigned + "." * (MAX_MISSILES_PER_TARGET - t.missiles_assigned)
            row   = f"{t.label:<10} {score:>7.1f}  {bar}"
            col   = (255, 70, 70) if t.missiles_assigned >= MAX_MISSILES_PER_TARGET \
                    else COL_PARAM_BRIGHT
            s.blit(self.font_small.render(row, True, col),
                   (WIDTH - 275, HEIGHT - 122 + i * 17))

        # ── Legend ────────────────────────────────────────────────
        legend = [("S1", COL_MISSILE,   " yellow missile"),
                  ("S2", COL_MISSILE_2, " cyan missile")]
        for i, (sym, col, lbl) in enumerate(legend):
            s.blit(self.font_small.render(sym, True, col),
                   (WIDTH - 275, HEIGHT - 228 + i * 15))
            s.blit(self.font_small.render(lbl, True, COL_PARAM_DIM),
                   (WIDTH - 258, HEIGHT - 228 + i * 15))

        # ── Controls (top-left) ───────────────────────────────────
        ctrls = [
            "[T] Spawn + Auto-Alloc",
            "[A] Allocate All Threats",
            "[F/SPC] Manual Fire",
            "[R] Reset  [ESC] Quit",
        ]
        for i, cl in enumerate(ctrls):
            s.blit(self.font_small.render(cl, True, (75, 145, 75)), (12, 12 + i * 14))

        # Speed / Pause panel
        self.draw_speed_panel()

    # ── Main Loop ─────────────────────────────────────────────────

    def run(self):
        running    = True
        launch_pos = pygame.Vector2(LAUNCHER_X, LAUNCHER_Y)

        while running:
            raw_dt = self.clock.tick(FPS) / 1000.0   # real wall-clock seconds

            # ── Events ──────────────────────────────────────────
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key == pygame.K_p:
                        # Toggle pause
                        self.paused = not self.paused
                    elif event.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                        self.speed_idx = min(self.speed_idx + 1, len(SPEED_STEPS) - 1)
                    elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        self.speed_idx = max(self.speed_idx - 1, 0)
                    elif event.key == pygame.K_0:
                        self.speed_idx = SPEED_DEFAULT
                    elif event.key == pygame.K_t:
                        self.spawn_target()
                    elif event.key == pygame.K_a:
                        self.allocator.allocate(self.targets, self.missiles, launch_pos)
                    elif event.key in (pygame.K_SPACE, pygame.K_f):
                        self.manual_launch()
                    elif event.key == pygame.K_r:
                        self.reset()

            # ── Simulation step (skipped when paused) ───────────
            if not self.paused:
                # Sub-step to keep physics stable at high speeds
                sub_steps = max(1, int(self.game_speed))
                sub_dt    = raw_dt * self.game_speed / sub_steps

                for _ in range(sub_steps):
                    # Drain allocator launch queue
                    new_missiles = self.allocator.tick(self.targets, self.missiles, launch_pos)
                    self.missiles.extend(new_missiles)

                    for t in self.targets:  t.update(sub_dt)
                    for m in self.missiles: m.update(sub_dt)
                    for p in self.particles: p.update()

                    self.check_hits()

                # Recount missile assignments
                for t in self.targets:
                    if t.alive:
                        t.missiles_assigned = sum(
                            1 for m in self.missiles if m.alive and m.target is t)

                # Prune dead objects
                self.targets   = [t for t in self.targets   if t.alive]
                self.missiles  = [m for m in self.missiles  if m.alive]
                self.particles = [p for p in self.particles if not p.dead]

            # ── Render ──────────────────────────────────────────
            self.screen.fill(BACKGROUND)

            # Stars (deterministic)
            rng = random.Random(42)
            for _ in range(80):
                sx, sy = rng.randint(0, WIDTH), rng.randint(0, HEIGHT // 2)
                br     = rng.randint(60, 200)
                pygame.draw.circle(self.screen, (br, br, br), (sx, sy), 1)

            # Terrain
            pygame.draw.polygon(self.screen, (40, 45, 45), self.terrain)
            pygame.draw.lines(self.screen, (70, 80, 80), False, self.terrain[1:-1], 2)

            # Engagement lines (behind entities)
            self.draw_engagement_lines()

            # Launcher
            draw_launcher(self.screen, self.font_small)

            # Entities
            for t in self.targets:
                t.draw(self.screen, self.font_param, self.font_label)
            for m in self.missiles:
                m.draw(self.screen, self.font_param, self.font_label)

            # Particles
            for p in self.particles:
                p.draw(self.screen)

            # HUD overlays
            self.draw_hud()

            # ── Pause overlay ────────────────────────────────────
            if self.paused:
                # Dim the whole screen
                dim = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
                dim.fill((0, 0, 0, 60))
                self.screen.blit(dim, (0, 0))
                # Big centred banner
                big = self.font_title.render("-- PAUSED --", True, COL_PAUSE_TEXT)
                bx  = WIDTH  // 2 - big.get_width()  // 2
                by  = HEIGHT // 2 - big.get_height() // 2
                bg  = pygame.Surface((big.get_width() + 28, big.get_height() + 14),
                                     pygame.SRCALPHA)
                bg.fill((10, 10, 10, 210))
                self.screen.blit(bg, (bx - 14, by - 7))
                self.screen.blit(big, (bx, by))
                hint = self.font_hud.render("Press  P  to resume", True, (160, 160, 160))
                self.screen.blit(hint,
                    (WIDTH // 2 - hint.get_width() // 2, by + big.get_height() + 16))

            pygame.display.flip()

        pygame.quit()
        sys.exit()


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sim = Simulation()
    for _ in range(3):
        sim.spawn_target()
    sim.run()