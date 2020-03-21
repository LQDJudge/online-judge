import json
from channels.generic.websocket import AsyncWebsocketConsumer
from .models import Message
from .views import format_time
from django.urls import reverse
from django.http import HttpResponse, HttpResponseRedirect


from judge.models.profile import Profile


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_name = 'room'
        self.room_group_name = 'chat_%s' % self.room_name

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.accept()

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

    # Receive message from WebSocket
    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message = text_data_json['message']

        # Send message to room group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message,
            },
        )

    # Receive message from room group
    async def chat_message(self, event):
        message = event['message']
        time = save_data_and_get_time(message)
        message['time'] = format_time(time)
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'message': message,
        }))


# return time
def save_data_and_get_time(message):
    new_message = Message(body=message['body'],
                          author=Profile.objects
                                        .get(id=message['author_id']),
                          )
    new_message.save()
    HttpResponseRedirect(reverse('chat'))
    return new_message.time
