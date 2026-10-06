import json

from openai import OpenAI


class OpenAICustomerRequestClient:
    def __init__(
        self,
        model: str = "gpt-6-luna",
    ) -> None:
        self.client = OpenAI()
        self.model = model

    def interpret_customer_request(
        self,
        message: str,
    ) -> dict:
        response = self.client.responses.create(
            model=self.model,
            instructions=(
                "Tulkitse autokorjaamon asiakkaan viesti rakenteiseksi dataksi. "
                "Älä diagnosoi asiakkaan kuvaamista oireista vikaa tai tarvittavaa "
                "korjausta. Tunnista työ vain, jos asiakas itse selvästi ilmoittaa "
                "halutun työn tai vaihdettavan osan. "
                "Jos asiakas pyytää vian selvittämistä tai kuvaa oiretta, kuten "
                "outoa ääntä, tärinää, hajua, varoitusvaloa tai poikkeavaa "
                "käyttäytymistä, aseta diagnosis_required arvoksi true ja "
                "status-arvoksi HUMAN_REQUIRED. "
                "Diagnoositarpeesta ei saa päätellä korjaustoimenpidettä tai "
                "varaosia. "
                "Jos diagnoosia ei tarvita, aseta diagnosis_required arvoksi false. "
                "Jos työ, osa tai muu automaattiseen käsittelyyn tarvittava tieto "
                "on epävarma, käytä status-arvoa HUMAN_REQUIRED. "
                "Älä keksi rekisteritunnusta, työtä, akselia tai varaosia. "
                "Käytä operation-arvoa BRAKE_REPLACEMENT vain selvästi ilmoitetulle "
                "jarruosien vaihdolle. "
                "Käytä axle-arvoja FRONT, REAR, BOTH tai UNKNOWN. "
                "Käytä part_types-arvona tunnettua kanonista osatyyppiä, esimerkiksi "
                "brake_pad. "
                "Palauta vain JSON-objekti, jossa ovat kentät registration, "
                "operation, axle, part_types, diagnosis_required ja status. "
                "operation voi olla null. "
                "diagnosis_required on true tai false. "
                "status on READY tai HUMAN_REQUIRED."
            ),
            input=message,
        )

        data = json.loads(
            response.output_text
        )

        if not isinstance(data, dict):
            raise ValueError(
                "OpenAI customer request response must be a JSON object"
            )

        return data
