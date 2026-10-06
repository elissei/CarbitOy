import json

from openai import OpenAI

from domain.models.quote_presentation import QuotePresentationInput


class OpenAIQuotePresentationClient:
    def __init__(
        self,
        model: str = "gpt-6-luna",
    ) -> None:
        self.client = OpenAI()
        self.model = model

    def create_work_description(
        self,
        presentation_input: QuotePresentationInput,
    ) -> str:
        facts = {
            "operation": presentation_input.operation,
            "axle": presentation_input.axle,
            "part_types": presentation_input.part_types,
        }

        response = self.client.responses.create(
            model=self.model,
            instructions=(
                "Kirjoita autokorjaamon asiakkaalle lyhyt ja luonnollinen "
                "suomenkielinen työn kuvaus. "
                "Muotoile vain annetut faktat. "
                "Älä lisää työtä, varaosia, hintoja, työaikaa tai muita "
                "faktoja, joita rakenteisessa datassa ei ole. "
                "Palauta vain työn kuvaus ilman muuta tekstiä."
            ),
            input=json.dumps(
                facts,
                ensure_ascii=False,
            ),
        )

        work_description = response.output_text.strip()

        if not work_description:
            raise ValueError(
                "OpenAI returned an empty work description"
            )

        return work_description
