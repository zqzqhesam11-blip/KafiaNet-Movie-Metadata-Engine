import flet as ft


def status_bar():

    return ft.Row(

        [

            ft.Container(

                width=180,
                height=55,

                bgcolor="#121520",

                border_radius=18,

                padding=12,

                content=ft.Row(
                    [
                        ft.Text(
                            "🔌",
                            size=22
                        ),

                        ft.Text(
                            "USB",
                            color="white"
                        )
                    ]
                )

            ),



            ft.Container(

                width=180,
                height=55,

                bgcolor="#121520",

                border_radius=18,

                padding=12,

                content=ft.Row(
                    [
                        ft.Text(
                            "📡",
                            size=22
                        ),

                        ft.Text(
                            "WIRELESS",
                            color="white"
                        )
                    ]
                )

            ),



            ft.Container(

                width=200,
                height=55,

                bgcolor="#121520",

                border_radius=18,

                padding=12,

                content=ft.Row(
                    [
                        ft.Text(
                            "💾",
                            size=22
                        ),

                        ft.Text(
                            "Storage",
                            color="white"
                        )
                    ]
                )

            )

        ],

        spacing=15

    )
