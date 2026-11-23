import flet as ft
from pathlib import Path
from .images import IMAGE_NAMES


GAME_COLORS = {
    "Fortnite": "#2D8CFF",
    "Genshin Impact": "#8A5CFF",
    "eFootball": "#00B894",
    "Free Fire": "#FF9F1C",
    "PUBG Mobile": "#F5C542",
    "Call of Duty Mobile": "#FF4D4D",
    "Grand Theft Auto": "#9B59B6"
}


def game_card(name, icon):

    color = GAME_COLORS.get(name, "#00E5FF")

    image_path = Path(
        "assets/games/" + IMAGE_NAMES.get(name, "")
    )


    if image_path.exists():

        image = ft.Image(
            src=str(image_path),
            width=140,
            height=140
        )

    else:

        image = ft.Text(
            icon,
            size=70
        )


    return ft.Container(

        width=250,

        height=300,

        bgcolor="#151927",

        border_radius=25,

        padding=15,

        border=ft.Border.all(
            1,
            color
        ),


        content=ft.Column(

            [

                image,


                ft.Text(

                    name,

                    size=22,

                    color="white",

                    weight=ft.FontWeight.BOLD

                )

            ],


            horizontal_alignment=ft.CrossAxisAlignment.CENTER

        )

    )
