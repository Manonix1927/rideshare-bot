from aiogram.fsm.state import State, StatesGroup


class DriverStates(StatesGroup):
    from_address = State()
    to_address = State()
    departure_time = State()
    price = State()
    seats = State()


class PassengerStates(StatesGroup):
    from_address = State()
    to_address = State()
    departure_time = State()
    budget = State()
    passengers_count = State()


class EditTripStates(StatesGroup):
    choosing_field = State()
    editing_from = State()
    editing_to = State()
    editing_time = State()
    editing_price = State()
    editing_seats = State()


class SupportStates(StatesGroup):
    choosing_type = State()
    writing_message = State()


class RatingStates(StatesGroup):
    choosing_score = State()


class AdminStates(StatesGroup):
    editing_faq_question = State()
    editing_faq_answer = State()
    replying_to_ticket = State()
