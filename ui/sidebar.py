import flet as ft


def sidebar():

    return ft.Container(

        width=220,

        height=700,

        bgcolor="#10131C",

        border_radius=25,

        padding=18,


        content=ft.Column(

            spacing=14,

            controls=[


                ft.Text(
                    "KAFIA NET",
                    size=26,
                    color="#00E5FF",
                    weight=ft.FontWeight.BOLD
                ),


                ft.Text(
                    "CONTROL PRO",
                    size=16,
                    color="#AAAAAA"
                ),


                ft.Divider(),



                ft.Button(
                    "🎮 الألعاب",
                    width=180
                ),


                ft.Button(
                    "📂 نقل الملفات",
                    width=180
                ),


                ft.Button(
                    "📦 التثبيت",
                    width=180
                ),


                ft.Button(
                    "📜 سجل النقل",
                    width=180
                ),


                ft.Button(
                    "💾 التخزين",
                    width=180
                ),


                ft.Button(
                    "⚙ الإعدادات",
                    width=180
                ),



                ft.Container(
                    expand=True
                ),



                ft.Text(
                    "هشام الريمي",
                    color="#777777",
                    size=14
                )

            ]

        )

    )
