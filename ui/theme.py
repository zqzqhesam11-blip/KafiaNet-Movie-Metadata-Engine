import flet as ft


BG = "#070910"
CARD = "#121520"

NEON_BLUE = "#1DA1F2"

TEXT = "#FFFFFF"


def neon_shadow(color):

    return ft.BoxShadow(
        blur_radius=18,
        spread_radius=1,
        color="#303040",
        offset=ft.Offset(0, 6)
    )
