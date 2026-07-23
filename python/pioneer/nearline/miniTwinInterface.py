# This is the basic minitwin interface




class miniTwinInterface:
    def __init__(self):
        self.has_new_context = False # This is used as mock-up.
        self.last_context = "None"

    def AddContext(self, ctxt):
        # This function should actually pass the context to the minitwin
        print("Received context: ", ctxt)
        self.has_new_context = True
        self.last_context = ctxt

    def NextConfiguration(self):
        if not self.has_new_context:
            return []
        # Poll minitwin here.
        cfg = {
            "p1" : str(self.last_context),
            "p2" : "Nice day for testing."
        }

        self.has_new_context = False
        return [cfg]
