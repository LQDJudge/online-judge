def can_use_ai_features(user):
    if not user or not user.is_authenticated:
        return False
    return user.is_superuser or user.has_perm("judge.use_ai_features")
