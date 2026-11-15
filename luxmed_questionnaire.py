"""Follow the Patient Portal's single-choice questionnaire graph in memory."""

import uuid
from dataclasses import dataclass
from html.parser import HTMLParser


class QuestionnaireError(ValueError):
    pass


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "div", "li", "ul", "ol"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        self.handle_starttag(tag, ())

    def handle_data(self, data):
        self.parts.append(data)


def visible_text(value) -> str:
    if not isinstance(value, str):
        return ""
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return "\n".join(line for part in "".join(parser.parts).splitlines() if (line := " ".join(part.split())))


@dataclass(frozen=True, repr=False)
class QuestionnaireReceipt:
    process_id: str
    execution_id: str


class Questionnaire:
    def __init__(self, form: dict, service_id: int, city_id: int, now: float):
        self.form_id = form.get("id")
        source = form.get("inputService")
        questions = form.get("questions")
        if (
            not isinstance(self.form_id, str) or not self.form_id
            or not isinstance(source, dict) or source.get("servaId") != service_id
            or not isinstance(questions, list) or not questions
        ):
            raise QuestionnaireError()
        self.questions = {}
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("id"), str):
                raise QuestionnaireError()
            key = question["id"]
            if not key or key in self.questions:
                raise QuestionnaireError()
            self.questions[key] = question
        self.receipt = QuestionnaireReceipt(str(uuid.uuid4()), str(uuid.uuid4()))
        self.service_id = service_id
        self.city_id = city_id
        self.started = now
        self.visited = set()
        self.answer_ids = []
        self.choices = []
        self.question = {}
        self.result = None
        self.information = ""
        self._move(questions[0]["id"])

    def _move(self, question_id) -> None:
        # The portal follows a hidden condition node's single configured link.
        # Visible nodes always require a choice from the user.
        while True:
            if not isinstance(question_id, str) or question_id in self.visited or question_id not in self.questions:
                raise QuestionnaireError()
            question = self.questions[question_id]
            self.visited.add(question_id)
            answers = question.get("answers")
            if (
                not isinstance(answers, list) or not answers
                or any(not isinstance(answer, dict) or not isinstance(answer.get("id"), str)
                       or not answer["id"] or type(answer.get("order")) is not int for answer in answers)
                or len({answer["order"] for answer in answers}) != len(answers)
                or len({answer["id"] for answer in answers}) != len(answers)
            ):
                raise QuestionnaireError()
            kind = question.get("questionType")
            if kind == 5 and len(answers) == 1 and answers[0].get("triageResult") is None:
                connector = answers[0]
                self.answer_ids.append(connector["id"])
                question_id = connector.get("nextQuestionUuid")
                continue
            if kind not in (1, 4) or not visible_text(question.get("name")):
                raise QuestionnaireError()
            self.question = question
            self.choices = sorted(answers, key=lambda answer: answer["order"])
            return

    def prompt(self) -> str:
        info = visible_text(self.question.get("additionalInfo"))
        labels = [visible_text(answer.get("name")) for answer in self.choices]
        if not all(labels):
            raise QuestionnaireError()
        lines = [visible_text(self.question["name"]), info, ""]
        lines.extend(f"{number}. {label}" for number, label in enumerate(labels, 1))
        text = "\n".join(lines)
        if len(text.encode("utf-16-le")) > 6700:
            raise QuestionnaireError()
        return text

    def choose(self, number: int) -> None:
        if self.result is not None or not 1 <= number <= len(self.choices):
            raise QuestionnaireError()
        answer = self.choices[number - 1]
        self.answer_ids.append(answer["id"])
        outcome = answer.get("triageResult")
        if isinstance(outcome, dict):
            self.result = outcome
            self.information = visible_text(answer.get("finalInfo") or outcome.get("value") or outcome.get("message"))
        elif outcome is None:
            self._move(answer.get("nextQuestionUuid"))
        else:
            raise QuestionnaireError()

    def permits_original_service(self) -> bool:
        return (
            self.result is not None
            and self.result.get("discriminator") == "ServiceVariantResultDto"
            and type(self.result.get("servaId")) is int and self.result["servaId"] == self.service_id
        )

    def submission(self, now: float) -> dict:
        if not self.permits_original_service():
            raise QuestionnaireError()
        return {
            "locationId": self.city_id, "serviceVariantId": self.service_id, "process": 4,
            "processId": self.receipt.process_id, "executionId": self.receipt.execution_id,
            "triageId": self.form_id, "duration": max(0, round(now - self.started)),
            "result": self.result, "selectedAnswers": [{"id": answer_id} for answer_id in self.answer_ids],
        }
