from domain.models.customer_request import CustomerRequestInterpretation


READY_DEMO_MESSAGE = "ABC-123, etujarrupalat pitäisi vaihtaa"
DIAGNOSIS_DEMO_MESSAGE = "ABC-123, autosta kuuluu outoa ääntä edestä"


class DemoReplayInterpreter:
    def interpret(self, message: str) -> CustomerRequestInterpretation:
        message = message.strip()

        if message == READY_DEMO_MESSAGE:
            return CustomerRequestInterpretation.model_validate(
                {
                    "registration": "ABC-123",
                    "operation": "BRAKE_REPLACEMENT",
                    "axle": "FRONT",
                    "part_types": ["brake_pad"],
                    "diagnosis_required": False,
                    "status": "READY",
                }
            )

        if message == DIAGNOSIS_DEMO_MESSAGE:
            return CustomerRequestInterpretation.model_validate(
                {
                    "registration": "ABC-123",
                    "operation": None,
                    "axle": "FRONT",
                    "part_types": [],
                    "diagnosis_required": True,
                    "status": "HUMAN_REQUIRED",
                }
            )

        raise ValueError(
            "Tallennettu demotila tukee vain ennalta määriteltyjä "
            "esimerkkiviestejä. Valitse esimerkki tai käytä OpenAI-tilaa."
        )
