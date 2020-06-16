import json
from channels.generic.websocket import AsyncWebsocketConsumer
from .models import Message
from django.urls import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.core import serializers

from judge.jinja2.gravatar import gravatar
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

        author = self.scope['user']
        author = Profile.objects.get(user_id=author.id)

        message['author'] = author.username
        message['css_class'] = author.css_class
        message['image'] = gravatar(author, 32)

        message_saved = save_data_and_return(message, author)
        message['time'] = message_saved[0]['fields']['time']
        message['id'] = message_saved[0]['pk']

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
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'message': message,
        }))


# return time
def save_data_and_return(message, author):
    new_message = Message(body=message['body'],
                          author=author,
                          )
    new_message.save()
    json_data = serializers.serialize("json", 
                                        Message.objects
                                               .filter(pk=new_message.id)
                                      )
    return json.loads(json_data)