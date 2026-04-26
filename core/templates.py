"""
Manages template files
"""
from core.filemanager import FileManager


class TemplateManager:
    """
    Template manager file
    """
    @staticmethod
    def get_template(category, template="basic", output_json=False):
        """
        Reads a specific text file with arguments
        TODO: switch to improved FileManager
        """
        path = f"templates/{category}/{template}.txt"
        if output_json:
            return FileManager.load_json_file(path)
        entries = []
        for line in FileManager.read_file(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            entries.append(line)
        return entries
