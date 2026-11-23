import flet as ft
import json
from pathlib import Path

from .game_card import game_card


def dashboard():

    games_file = Path("config/games.json")

    games = []

    if games_file.exists():

        with open(
            games_file,
            "r",
            encoding="utf-8-sig"
        ) as f:

            games = json.load(f)


    cards = []


    icons = {

        "Fortnite":"🎯",
        "Genshin Impact":"🌌",
        "eFootball":"⚽",
        "Free Fire":"🔥",
        "PUBG Mobile":"🎮",
        "Call of Duty Mobile":"⚔",
        "Grand Theft Auto":"🚗"

    }


    for name,data in games.items():

        cards.append(

            game_card(

                name,

                icons.get(
                    name,
                    "🎮"
                )

            )

        )


    return ft.Column(

        [

            ft.Text(

                "🎮 مكتبة الألعاب",

                size=36,

                color="#00E5FF",

                weight=ft.FontWeight.BOLD

            ),


            ft.Row(

                cards,

                wrap=True,

                spacing=35,

                run_spacing=35

            )

        ]

    )
