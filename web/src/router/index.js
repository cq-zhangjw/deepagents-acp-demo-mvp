import { createRouter, createWebHashHistory } from 'vue-router';

const routes = [
  {
    path: '/',
    name: 'chat',
    component: () => import("../pages/ChatPage.vue")
  },
  {
    path: '/voice',
    name: 'voice-call',
    component: () => import("../pages/VoiceCallPage.vue")
  }
];

const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes
});

export default router;