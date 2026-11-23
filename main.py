import flet as ft

from ui.dashboard import dashboard
from ui.sidebar import sidebar
from ui.status_bar import status_bar


def main(page: ft.Page):

    page.title = "KAFIA NET CONTROL PRO"

    page.window_width = 1600
    page.window_height = 950

    page.bgcolor = "#050509"


    header = ft.Row(
        [
            ft.Text(
                "KAFIA NET CONTROL PRO",
                size=36,
                color="#00E5FF",
                weight=ft.FontWeight.BOLD
            ),

            ft.Row(
                [
                    ft.Button("USB"),
                    ft.Button("WIRELESS"),
                    ft.Button("💾 التخزين")
                ],
                spacing=10
            )
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN
    )


    body = ft.Row(
        [
            sidebar(),

            ft.Container(
                expand=True,
                padding=30,
                content=dashboard()
            )
        ],

        spacing=25
    )


    page.add(

        ft.Column(
            [
                header,

                ft.Divider(),

                body
            ]
        )
    )


ft.run(main)
